#!/usr/bin/env bash
# scripts/up.sh
# Идемпотентный запуск собственного проекта; shared lifecycle и чужие credentials не изменяются.
set +x
set -Eeuo pipefail
cd "$(dirname "$0")/.."
umask 077
current_stage='проверка инструментов'
finish() {
  # Удаляет секреты из памяти shell; выводит только название неуспешного этапа.
  local code=$?
  unset broker_config broker_values broker_password
  if [[ $code -ne 0 ]]; then printf 'Ошибка: %s (код %s).\n' "$current_stage" "$code" >&2; fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
stage() {
  # Пользователь видит текущий этап и обычные логи Docker без скрытого redirect.
  current_stage=$1
  printf '[%s; +%ss] %s\n' "$(date +%T)" "$SECONDS" "$current_stage"
}
for tool in docker timeout flock ss; do
  command -v "$tool" >/dev/null || { echo "Не найдена команда $tool: нужны Docker/Compose, coreutils, util-linux, iproute2." >&2; exit 1; }
done
mkdir -p .data
exec 9>.data/.deploy.lock
flock -n 9 || { echo 'Уже выполняется scripts/up.sh.' >&2; exit 1; }
stage 'Docker, Compose и существующая сеть ai-shared'
docker info >/dev/null
docker compose version
docker network inspect ai-shared >/dev/null
stage 'Сборка собственного закреплённого образа'
docker build --tag document-intake-ai:local .
stage 'Безопасное создание собственного sparse .env'
docker run --rm --user "$(id -u):$(id -g)" --mount "type=bind,source=$PWD,target=/work" \
  --workdir /work document-intake-ai:local python -m app.operations init-env
dc=(docker compose --env-file .env.example --env-file .env)
"${dc[@]}" config --quiet
stage 'Валидация Pydantic Settings без вывода секретов'
"${dc[@]}" run --rm --no-deps -T api python -m app.operations config
app_port="$("${dc[@]}" run --rm --no-deps -T api python -m app.operations port)"
[[ "$app_port" =~ ^[0-9]+$ ]] || { echo 'Некорректный APP_PORT.' >&2; exit 1; }
stage "Проверка порта $app_port"
if [[ -n "$(ss -H -ltn "sport = :$app_port")" ]]; then
  owned_api="$("${dc[@]}" ps -q api)"
  if [[ -z "$owned_api" ]] || ! docker port "$owned_api" "$app_port/tcp" >/dev/null 2>&1; then
    echo "APP_PORT=$app_port занят другим сервисом. Измените настройку явно; порт не переключается автоматически." >&2
    exit 1
  fi
fi
broker_config="$("${dc[@]}" run --rm --no-deps -T api python -m app.operations broker-config)"
mapfile -t broker_values <<< "$broker_config"
[[ ${#broker_values[@]} -eq 5 && ${broker_values[4]} =~ ^[0-9]+$ ]] || { echo 'Неверный протокол настроек broker.' >&2; exit 1; }
broker_container=${broker_values[0]}
broker_user=${broker_values[1]}
broker_vhost=${broker_values[2]}
broker_password=${broker_values[3]}
broker_timeout=${broker_values[4]}
bounded_docker() {
  # Ограничивает RPC и зависание Docker-клиента; пароль не передаётся аргументом.
  timeout --signal=TERM --kill-after=5s "$((broker_timeout + 15))s" docker "$@"
}
stage 'Проверка существующего RabbitMQ в ai-shared'
[[ "$(bounded_docker inspect --format '{{.State.Running}}' "$broker_container")" == true ]] || { echo 'Shared RabbitMQ не работает.' >&2; exit 1; }
members="$(bounded_docker network inspect ai-shared --format '{{range .Containers}}{{println .Name}}{{end}}')"
awk -v name="$broker_container" '$0 == name { found=1 } END { exit !found }' <<< "$members" || { echo 'RabbitMQ отсутствует в ai-shared.' >&2; exit 1; }
rabbitmq_ctl() {
  # Использует локальную cookie существующего RabbitMQ; admin password не требуется.
  bounded_docker exec --user rabbitmq "$broker_container" timeout --signal=TERM --kill-after=3s \
    "${broker_timeout}s" rabbitmqctl --quiet --timeout "$broker_timeout" "$@"
}
rabbitmq_secret() {
  # Секрет идёт stdin → приватный RAM-файл; eval содержит только binding пути и имени.
  local expression
  case "$1" in
    add_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_auth_backend_internal:add_user(_1, P, <<"document-intake-bootstrap">>) of ok -> ok; _ -> throw(user_creation_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    authenticate_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_access_control:check_user_pass_login(_1, P) of {ok, _} -> ok; _ -> throw(authentication_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    *) return 2 ;;
  esac
  printf '%s' "$broker_password" | bounded_docker exec -i --user rabbitmq "$broker_container" \
    timeout --signal=TERM --kill-after=3s "$((broker_timeout + 8))s" sh -c '
      set -eu
      umask 077
      secret_file=$(mktemp /dev/shm/document-intake-password.XXXXXX)
      trap '\''rm -f "$secret_file"'\'' EXIT
      trap '\''exit 143'\'' TERM
      trap '\''exit 130'\'' INT
      cat > "$secret_file"
      timeout --signal=TERM --kill-after=3s "${1}s" rabbitmqctl --quiet --timeout "$1" eval "$2" "$3" "$secret_file"
    ' -- "$broker_timeout" "$expression" "$broker_user"
}
stage 'Проверка выделенного RabbitMQ user и пароля без изменения credentials'
users="$(rabbitmq_ctl list_users --no-table-headers)"
record="$(awk -v name="$broker_user" '$1 == name { print; exit }' <<< "$users")"
if [[ -n "$record" ]]; then
  awk '$2 == "[]" { valid=1 } END { exit !valid }' <<< "$record" || { echo 'У пользователя служебные теги; нужна отдельная учётная запись.' >&2; exit 1; }
else
  rabbitmq_secret add_user
fi
if ! rabbitmq_secret authenticate_user; then
  echo 'Пароль существующего RabbitMQ user не подтверждён. Проверьте свой RABBITMQ_PASSWORD. Пароль не изменялся.' >&2
  exit 1
fi
permissions="$(rabbitmq_ctl list_user_permissions "$broker_user" --no-table-headers)"
awk -v name="$broker_vhost" 'NF && $1 != name { foreign=1 } END { exit foreign }' <<< "$permissions" || { echo 'У user есть права в чужом vhost. Нужна отдельная учётная запись.' >&2; exit 1; }
vhosts="$(rabbitmq_ctl list_vhosts name --no-table-headers)"
if ! awk -v name="$broker_vhost" '$0 == name { found=1 } END { exit !found }' <<< "$vhosts"; then
  rabbitmq_ctl add_vhost "$broker_vhost"
fi
permissions="$(rabbitmq_ctl list_permissions -p "$broker_vhost" --no-table-headers)"
awk -v name="$broker_user" 'NF && $1 != name { foreign=1 } END { exit foreign }' <<< "$permissions" || { echo 'Выбранный vhost используется другими users. Требуется выделенный namespace.' >&2; exit 1; }
if ! awk -v name="$broker_user" '$1 == name && $2 == ".*" && $3 == ".*" && $4 == ".*" { valid=1 } END { exit !valid }' <<< "$permissions"; then
  rabbitmq_ctl set_permissions -p "$broker_vhost" "$broker_user" '.*' '.*' '.*'
fi
permissions="$(rabbitmq_ctl list_user_permissions "$broker_user" --no-table-headers)"
awk -v name="$broker_vhost" 'NF && ($1 != name || $2 != ".*" || $3 != ".*" || $4 != ".*") { invalid=1 } $1 == name { found=1 } END { exit invalid || !found }' <<< "$permissions" || { echo 'Права собственного vhost не подтверждены.' >&2; exit 1; }
unset broker_config broker_values broker_password
stage 'Проверка реальных контрактов Docling OCR и VLM vision/JSON'
"${dc[@]}" run --rm --no-deps -T api python -m app.operations contracts
stage 'Собственная PostgreSQL и Alembic migrations'
"${dc[@]}" up -d --wait --wait-timeout 120 postgres
"${dc[@]}" run --rm --no-deps -T migrate
stage 'Запуск или обновление собственных API/worker'
"${dc[@]}" up -d --wait --wait-timeout 180 api worker
stage 'Реальный AMQP, consumer и readiness'
"${dc[@]}" exec -T api python -m app.operations broker-check
"${dc[@]}" exec -T api python -m app.operations ready
stage 'HTTP по внешнему IP: web, API, pipeline, PDF и checksum'
"${dc[@]}" exec -T api python -m app.operations smoke
"${dc[@]}" ps
"${dc[@]}" logs --tail=30 api worker
public_url="$("${dc[@]}" exec -T api python -m app.operations public-url)"
printf 'Сервис проверен с Docker-хоста: %s/\nДокументация API: %s/docs\n' "$public_url" "$public_url"
echo 'Проверьте доступ из другой разрешённой LAN/VPN-машины. Firewall автоматически не изменялся.'
