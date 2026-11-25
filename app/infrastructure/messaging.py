# app/infrastructure/messaging.py
"""Выделенная durable AMQP-очередь с publisher confirms и ограниченной глубиной."""

from contextlib import asynccontextmanager

import aio_pika


class NoConsumer(RuntimeError):
    """Живой брокер не видит подписку; readiness может проверить работающий lease проекта."""


class Rabbit:
    """Работает только в настроенном собственном vhost; broker lifecycle не изменяет."""

    def __init__(self, settings) -> None:
        """Сохраняет настройки, не раскрывая AMQP credentials URL в логах."""
        self.settings = settings
        self.connection = None
        self.channel = None
        self.queue = None

    async def connect(self) -> None:
        """Выполняет реальную авторизацию и объявляет ограниченную очередь приложения."""
        self.connection = await aio_pika.connect_robust(
            host=self.settings.rabbitmq_host,
            port=self.settings.rabbitmq_port,
            login=self.settings.rabbitmq_user,
            password=self.settings.rabbitmq_password.get_secret_value(),
            virtualhost=self.settings.rabbitmq_vhost,
            timeout=self.settings.rabbitmq_timeout_seconds,
            reconnect_interval=5,
        )
        self.channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
        await self.channel.set_qos(prefetch_count=self.settings.worker_concurrency)
        self.queue = await self.channel.declare_queue(
            self.settings.rabbitmq_queue,
            durable=True,
            arguments={
                "x-max-length": self.settings.queue_max_depth,
                "x-overflow": "reject-publish",
            },
        )

    async def publish(self, job_id: str) -> None:
        """Передаёт только ID, не документы, OCR, секреты или долгоживущие результаты."""
        await self.channel.default_exchange.publish(
            aio_pika.Message(
                job_id.encode(),
                content_type="text/plain",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=self.settings.rabbitmq_queue,
            mandatory=True,
            timeout=self.settings.rabbitmq_timeout_seconds,
        )

    async def require_consumer(self) -> None:
        """Проверяет реальную подписку worker; отсутствие consumer не считается READY."""
        if self.connection is None or self.connection.is_closed:
            await self.connect()
        queue = await self.channel.declare_queue(self.settings.rabbitmq_queue, passive=True)
        if queue.declaration_result.consumer_count < 1:
            raise NoConsumer("Очередь приложения не имеет consumer")

    @asynccontextmanager
    async def delivery(self):
        """Выдаёт одну доставку с остановленной подпиской; publisher channel независим.

        Prefetch=1 не допускает второго сообщения до ACK. CancelOk получен до
        передачи сообщения вызывающему коду. Закрытие канала возвращает брокеру
        неопределённые доставки; delivery tag никогда не переиспользуется.
        """
        channel = await self.connection.channel(publisher_confirms=False)
        try:
            await channel.set_qos(prefetch_count=1)
            queue = await channel.declare_queue(self.settings.rabbitmq_queue, passive=True)
            async with queue.iterator() as iterator:
                message = await anext(iterator)
            yield message
        finally:
            await channel.close()

    async def close(self) -> None:
        """Закрывает только собственное соединение, сохраняя durable очередь."""
        if self.connection:
            await self.connection.close()
