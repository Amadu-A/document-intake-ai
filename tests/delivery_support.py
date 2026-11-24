# tests/delivery_support.py
"""Изолированный стенд доставки: собственные PG/RabbitMQ и worker с управляемой операцией."""

import asyncio
import json
import secrets
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import Settings
from app.domain.jobs import Job, utcnow
from app.infrastructure.database import Base, Database
from app.infrastructure.messaging import Rabbit

WORKER = '''"""Управляемая тестовая операция; доставка, claim, heartbeat и recovery — production."""
import asyncio
from pathlib import Path
from app.core.container import Container
from app.core.settings import Settings
from app.worker import run

class ControlledAnalysis:
    """Держит один slot до разрешения теста, не обращаясь к OCR/VLM."""
    def __init__(self, database): self.database = database
    async def execute(self, job):
        await self.database.checkpoint(
            job.id, job.fence, 'stage', {'stage':'delivery_test','progress':10})
        while not Path('/test-gates',job.id).exists(): await asyncio.sleep(.1)
        await self.database.finish(
            job.id, job.fence, {'schema_version':'1.0','batch_id':job.id,'files':[]},
            job.manifest,None,False)
    prepare = execute

async def main():
    container=Container(Settings());container.analysis=ControlledAnalysis(container.database)
    try: await run(container)
    finally: await container.close()
asyncio.run(main())
'''


def docker(*args: str) -> str:
    """Выполняет команду без shell; секретов в аргументах нет, вывод сохраняет pytest."""
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("NOT_RUN: нужен Docker CLI")
    return subprocess.run(  # noqa: S603 — только известные тесту аргументы, без shell.
        [executable, *args], check=True, capture_output=True, text=True, timeout=90
    ).stdout.strip()


def free_port() -> int:
    """Выбирает loopback-порт, который Docker закрепит явно и сохранит при restart."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class DeliveryLab:
    """Все изменяемые Docker-ресурсы имеют уникальный префикс и принадлежат этому тесту."""

    def __init__(self, root: Path, app_image: str = "document-intake-ai:local"):
        """Создаёт приватные тестовые secrets; настоящие .env не читаются."""
        self.root = root
        self.app_image = app_image
        self.prefix = "document-intake-delivery-" + uuid4().hex[:10]
        self.containers = []
        self.workers = []
        self.network_created = False
        self.password = secrets.token_hex(24)
        self.root.mkdir(parents=True, exist_ok=True)
        self.env = root / "test.env"
        values = {
            "POSTGRES_PASSWORD": self.password,
            "RABBITMQ_DEFAULT_USER": "document_intake",
            "RABBITMQ_DEFAULT_PASS": self.password,
            "RABBITMQ_DEFAULT_VHOST": "document-intake",
            "DATABASE_HOST": "pg",
            "DATABASE_NAME": "postgres",
            "DATABASE_USER": "postgres",
            "DATABASE_PASSWORD": self.password,
            "RABBITMQ_HOST": "mq",
            "RABBITMQ_PASSWORD": self.password,
            "RABBITMQ_TIMEOUT_SECONDS": "3",
            "SESSION_SECRET": secrets.token_hex(32),
            "DOWNLOAD_SECRET": secrets.token_hex(32),
            "API_KEYS": json.dumps({"test": secrets.token_hex(24)}),
            "WORKER_LEASE_SECONDS": "15",
            "DATA_DIR": "/test-gates/data",
            "PYTHONPATH": "/opt/app",
        }
        self.env.write_text("\n".join(f"{key}={value}" for key, value in values.items()), "utf-8")
        self.env.chmod(0o600)
        (root / "worker.py").write_text(WORKER, "utf-8")

    def setup(self) -> None:
        """Проверяет локальные образы и поднимает только собственные test PG/RabbitMQ."""
        for image in (
            "rabbitmq:4.1-management",
            "postgres:16.15-alpine",
            self.app_image,
        ):
            docker("image", "inspect", image)
        docker("network", "create", self.prefix)
        self.network_created = True
        for suffix, image, port in (
            ("pg", "postgres:16.15-alpine", "5432"),
            ("mq", "rabbitmq:4.1-management", "5672"),
        ):
            name = self.prefix + "-" + suffix
            args = [
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                self.prefix,
                "--network-alias",
                suffix,
                "--env-file",
                str(self.env),
                "-p",
                f"127.0.0.1:{free_port()}:{port}",
            ]
            if suffix == "mq":
                args += ["-p", f"127.0.0.1:{free_port()}:15672"]
            docker(*args, image)
            self.containers.append(name)
        self.pg_port = int(docker("port", self.prefix + "-pg", "5432/tcp").rsplit(":", 1)[1])
        self.mq_port = int(docker("port", self.prefix + "-mq", "5672/tcp").rsplit(":", 1)[1])
        self.http_port = int(docker("port", self.prefix + "-mq", "15672/tcp").rsplit(":", 1)[1])
        self.wait_ready()

    def wait_ready(self) -> None:
        """Ожидает actual readiness собственных контейнеров с конечным бюджетом."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                docker("exec", self.prefix + "-pg", "pg_isready", "-U", "postgres")
                with httpx.Client(trust_env=False, timeout=2) as client:
                    result = client.get(
                        f"http://127.0.0.1:{self.http_port}/api/overview",
                        auth=("document_intake", self.password),
                    )
                    if result.status_code == 200:
                        return
            except (subprocess.CalledProcessError, httpx.HTTPError):
                pass
            time.sleep(0.5)
        raise RuntimeError("Изолированные PG/RabbitMQ не готовы")

    def worker(self) -> str:
        """Запускает текущий production worker, подменяя только длительную бизнес-операцию."""
        name = self.prefix + "-worker-" + str(len(self.workers) + 1)
        project = Path(__file__).resolve().parents[1]
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            self.prefix,
            "--env-file",
            str(self.env),
            "--entrypoint",
            "/opt/app/.venv/bin/python",
            "--mount",
            f"type=bind,source={project / 'app'},target=/opt/app/app,readonly",
            "--mount",
            f"type=bind,source={project / 'prompts'},target=/opt/app/prompts,readonly",
            "--mount",
            f"type=bind,source={project / '.env.example'},target=/opt/app/.env.example,readonly",
            "--mount",
            f"type=bind,source={self.root},target=/test-gates",
            self.app_image,
            "/test-gates/worker.py",
        )
        self.containers.append(name)
        self.workers.append(name)
        return name

    def command(self, operation: str, *names: str) -> None:
        """Сбой/перезапуск разрешён только для контейнеров, созданных данным экземпляром."""
        assert names and all(name in self.containers for name in names)
        assert operation in {"kill", "stop", "start", "restart"}
        docker(operation, *names)

    def cleanup(self) -> None:
        """Удаляет только ресурсы собственного стенда, включая его анонимные test volumes."""
        for name in reversed(self.containers):
            assert name.startswith(self.prefix + "-")
            try:
                docker("rm", "--force", "--volumes", name)
            except subprocess.CalledProcessError:
                pass
        if self.network_created:
            docker("network", "rm", self.prefix)

    async def open(self) -> None:
        """Создаёт настоящую PostgreSQL repository и AMQP adapter для тестовых запросов."""
        self.settings = Settings(
            _env_file=None,
            database_password=self.password,
            rabbitmq_password=self.password,
            rabbitmq_host="127.0.0.1",
            rabbitmq_port=self.mq_port,
            rabbitmq_timeout_seconds=3,
            worker_lease_seconds=15,
        )
        self.engine = create_async_engine(
            f"postgresql+asyncpg://postgres:{self.password}@127.0.0.1:{self.pg_port}/postgres",
            pool_pre_ping=True,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.database = Database(self.engine, self.settings)
        self.rabbit = Rabbit(self.settings)
        await self.rabbit.connect()

    async def close(self) -> None:
        """Освобождает только соединения текущего теста."""
        await self.rabbit.close()
        await self.engine.dispose()

    async def job(self) -> Job:
        """Создаёт durable queued job без документов: сценарий проверяет только доставку."""
        job = Job(
            id=str(uuid4()),
            owner="integration:delivery",
            status="queued",
            created_at=utcnow(),
            expires_at=None,
            sources=[],
            prompts={},
            preview_complete=True,
        )
        await self.database.create(job, None, uuid4().hex)
        return job

    async def info(self) -> dict:
        """Читает actual ready/unacked только своей тестовой очереди."""
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get(
                f"http://127.0.0.1:{self.http_port}/api/queues/document-intake/document-intake.jobs",
                auth=("document_intake", self.password),
            )
            response.raise_for_status()
            return response.json()

    async def wait_status(self, identifier: str, status: str, seconds: int = 40) -> Job:
        """Проверяет durable состояние, не принимая наличие сообщения за выполнение задания."""
        async with asyncio.timeout(seconds):
            while True:
                job = await self.database.get(identifier)
                if job.status == status:
                    return job
                await asyncio.sleep(0.2)

    async def ready(self, minimum: int = 1) -> dict:
        """Дожидается обновления management-счётчика, который RabbitMQ публикует с задержкой."""
        async with asyncio.timeout(30):
            while True:
                info = await self.info()
                if info.get("messages_ready", 0) >= minimum:
                    return info
                await asyncio.sleep(0.5)

    async def release(self, identifier: str) -> None:
        """Разрешает контролируемой операции завершиться, не вмешиваясь в delivery/claim."""
        await asyncio.to_thread((self.root / identifier).touch)


class LostConfirmProxy:
    """Пропускает настоящий AMQP, обрывая соединение при первом Basic.Ack от брокера."""

    def __init__(self, upstream_port: int):
        """Не пишет AMQP payload или credentials в файлы/логи."""
        self.upstream_port = upstream_port
        self.dropped = asyncio.Event()
        self.tasks = set()

    async def __aenter__(self):
        """Открывает только собственный loopback TCP listener."""
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *args):
        """Закрывает listener и все обслуженные им соединения, включая robust reconnect."""
        self.server.close()
        await self.server.wait_closed()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        """Передаёт клиентский поток и разбирает только заголовки серверных AMQP frames."""
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        pumping = None
        try:
            upstream, remote = await asyncio.open_connection("127.0.0.1", self.upstream_port)

            async def forward():
                """Проксирует клиентские байты без разбора и отражения секретов."""
                while data := await reader.read(65536):
                    remote.write(data)
                    await remote.drain()

            pumping = asyncio.create_task(forward())
            while True:
                header = await upstream.readexactly(7)
                kind, channel, size = struct.unpack(">BHI", header)
                payload = await upstream.readexactly(size + 1)
                if (
                    kind == 1
                    and channel
                    and payload[:4] == struct.pack(">HH", 60, 80)
                    and not self.dropped.is_set()
                ):
                    self.dropped.set()
                    return
                writer.write(header + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if pumping:
                pumping.cancel()
                await asyncio.gather(pumping, return_exceptions=True)
            writer.close()
            if remote:
                remote.close()
            self.tasks.discard(task)
