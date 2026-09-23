from __future__ import annotations

import argparse
import base64
import csv
import getpass
import http.client
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


BASE_DIR = Path(__file__).resolve().parent
PANELS_FILE = BASE_DIR / "panels.txt"
AUDIO_FILE = BASE_DIR / "9.wav"
STATE_FILE = BASE_DIR / "deployment_state.json"
LOG_FILE = BASE_DIR / "sokol_mass_audio.log"
CSV_LOG_FILE = BASE_DIR / "results.csv"

EVENT_BASE = "/v1/assistant/key/open/neutral"
ASSISTANT_SETTINGS = "/assistant/settings"
SYSTEM_INFO = "/system/info"
EXPECTED_AUDIO_NAME = "9.wav"
EXPECTED_REVISION = "rev.5"
DEFAULT_WORKERS = 20
REQUEST_TIMEOUT_SECONDS = 12
REQUEST_ATTEMPTS = 3


class Http10Connection(http.client.HTTPConnection):
    _http_vsn = 10
    _http_vsn_str = "HTTP/1.0"


@dataclass(frozen=True)
class Result:
    ip: str
    operation: str
    ok: bool
    status: int | None
    message: str


class PanelClient:
    def __init__(self, ip: str, username: str, password: str) -> None:
        self.ip = ip
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.authorization = f"Basic {token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        request_headers = {
            "Authorization": self.authorization,
            "Connection": "close",
            "Expect": "",
        }
        if headers:
            request_headers.update(headers)
        if body is not None:
            request_headers["Content-Length"] = str(len(body))

        last_error: Exception | None = None
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            connection = Http10Connection(self.ip, 80, timeout=REQUEST_TIMEOUT_SECONDS)
            try:
                connection.request(method, path, body=body, headers=request_headers)
                response = connection.getresponse()
                payload = response.read()
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                return response.status, payload, response_headers
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt < REQUEST_ATTEMPTS:
                    time.sleep(attempt)
            finally:
                connection.close()
        raise RuntimeError(f"нет ответа после {REQUEST_ATTEMPTS} попыток: {last_error}")

    def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        status, payload, _ = self.request("GET", path)
        if not payload:
            return status, {}
        return status, json.loads(payload.decode("utf-8"))

    def put_json(self, path: str, value: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status, payload, _ = self.request(
            "PUT",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        if not payload:
            return status, {}
        return status, json.loads(payload.decode("utf-8"))


_csv_lock = threading.Lock()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def append_result(result: Result) -> None:
    with _csv_lock:
        new_file = not CSV_LOG_FILE.exists()
        with CSV_LOG_FILE.open("a", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output, delimiter=";")
            if new_file:
                writer.writerow(["datetime", "ip", "operation", "ok", "http_status", "message"])
            writer.writerow(
                [
                    datetime.now().isoformat(timespec="seconds"),
                    result.ip,
                    result.operation,
                    "YES" if result.ok else "NO",
                    result.status if result.status is not None else "",
                    result.message,
                ]
            )


def read_ips() -> list[str]:
    if not PANELS_FILE.exists():
        raise FileNotFoundError(f"не найден файл {PANELS_FILE.name}")
    ips = []
    seen = set()
    for raw_line in PANELS_FILE.read_text(encoding="utf-8-sig").splitlines():
        ip = raw_line.strip()
        if not ip:
            continue
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ValueError(f"некорректный IP в panels.txt: {ip!r}") from exc
        if parsed.version != 4:
            raise ValueError(f"поддерживаются только IPv4-адреса: {ip!r}")
        normalized = str(parsed)
        if normalized not in seen:
            ips.append(normalized)
            seen.add(normalized)
    if not ips:
        raise RuntimeError("список panels.txt пуст")
    return ips


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"created_at": datetime.now().isoformat(), "panels": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def decode_error(payload: bytes) -> str:
    if not payload:
        return "пустой ответ"
    try:
        return json.dumps(json.loads(payload.decode("utf-8")), ensure_ascii=False)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload[:300].decode("utf-8", errors="replace")


def prepare_panel(ip: str, password: str, audio: bytes) -> tuple[Result, dict[str, Any] | None]:
    client = PanelClient(ip, "root", password)
    try:
        status, info = client.get_json(SYSTEM_INFO)
        if status != 200:
            return Result(ip, "prepare", False, status, f"system/info: {info}"), None
        revision = str(info.get("deviceModel") or info.get("deviceRevision") or "")
        if EXPECTED_REVISION.lower() not in revision.lower():
            return Result(ip, "prepare", False, status, f"пропущена панель {revision!r}"), None

        settings_status, settings = client.get_json(ASSISTANT_SETTINGS)
        if settings_status != 200:
            return Result(ip, "prepare", False, settings_status, f"assistant/settings: {settings}"), None

        list_status, listing = client.get_json(f"{EVENT_BASE}/list")
        if list_status != 200:
            return Result(ip, "prepare", False, list_status, f"список файлов: {listing}"), None
        old_files = [str(item.get("name")) for item in listing.get("files", []) if item.get("name")]
        obsolete_files = [name for name in old_files if name != EXPECTED_AUDIO_NAME]
        obsolete_deleted = False
        used_space_retry = False

        # Keep all other sounds until the new file is uploaded and verified.
        # This prevents a failed upload from leaving the event without audio.
        if EXPECTED_AUDIO_NAME in old_files:
            delete_body = json.dumps([EXPECTED_AUDIO_NAME], separators=(",", ":")).encode("utf-8")
            delete_status, delete_payload, _ = client.request(
                "DELETE",
                f"{EVENT_BASE}/list",
                body=delete_body,
                headers={"Content-Type": "application/json"},
            )
            if delete_status not in (200, 204):
                return Result(ip, "prepare", False, delete_status, f"удаление старого 9.wav: {decode_error(delete_payload)}"), None

        def upload_audio() -> tuple[int, bytes]:
            status_code, payload, _ = client.request(
                "PUT",
                f"{EVENT_BASE}/file",
                body=audio,
                headers={
                    "Content-Type": "audio/wav",
                    "Content-Disposition": 'attachment; filename="9.wav"',
                },
            )
            return status_code, payload

        upload_status, upload_payload = upload_audio()
        if upload_status != 200:
            upload_error = decode_error(upload_payload)
            normalized_error = upload_error.casefold().replace("ё", "е")
            no_space = "не достаточно места" in normalized_error or "недостаточно места" in normalized_error
            if not no_space:
                return Result(ip, "prepare", False, upload_status, f"загрузка: {upload_error}"), None
            if not obsolete_files:
                return Result(ip, "prepare", False, upload_status, f"нехватка места, удалять нечего: {upload_error}"), None

            delete_body = json.dumps(obsolete_files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            delete_status, delete_payload, _ = client.request(
                "DELETE",
                f"{EVENT_BASE}/list",
                body=delete_body,
                headers={"Content-Type": "application/json"},
            )
            if delete_status not in (200, 204):
                return Result(ip, "prepare", False, delete_status, f"очистка после нехватки места: {decode_error(delete_payload)}"), None
            obsolete_deleted = True
            used_space_retry = True

            upload_status, upload_payload = upload_audio()
            if upload_status != 200:
                return Result(
                    ip,
                    "prepare",
                    False,
                    upload_status,
                    f"повторная загрузка после очистки: {decode_error(upload_payload)}",
                ), None

        verify_status, verify = client.get_json(f"{EVENT_BASE}/list")
        files = verify.get("files", []) if verify_status == 200 else []
        expected = next((item for item in files if item.get("name") == EXPECTED_AUDIO_NAME), None)
        if not expected or int(expected.get("size", -1)) != len(audio):
            return Result(ip, "prepare", False, verify_status, f"проверка не пройдена: {verify}"), None

        if obsolete_files and not obsolete_deleted:
            delete_body = json.dumps(obsolete_files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            delete_status, delete_payload, _ = client.request(
                "DELETE",
                f"{EVENT_BASE}/list",
                body=delete_body,
                headers={"Content-Type": "application/json"},
            )
            if delete_status not in (200, 204):
                return Result(ip, "prepare", False, delete_status, f"удаление прежних файлов: {decode_error(delete_payload)}"), None

        final_status, final_listing = client.get_json(f"{EVENT_BASE}/list")
        final_files = final_listing.get("files", []) if final_status == 200 else []
        if len(final_files) != 1 or final_files[0].get("name") != EXPECTED_AUDIO_NAME:
            return Result(ip, "prepare", False, final_status, f"итоговая проверка не пройдена: {final_listing}"), None
        if int(final_files[0].get("size", -1)) != len(audio):
            return Result(ip, "prepare", False, final_status, f"неверный размер 9.wav: {final_listing}"), None

        state = {
            "mac": info.get("mac"),
            "deviceModel": revision,
            "originalAssistantSettings": settings,
            "originalOfflineFiles": old_files,
            "preparedAt": datetime.now().isoformat(timespec="seconds"),
            "audioSize": len(audio),
        }
        retry_note = " после очистки раздела" if used_space_retry else ""
        return Result(ip, "prepare", True, 200, f"9.wav загружен{retry_note}, размер {len(audio)}"), state
    except Exception as exc:  # noqa: BLE001 - result must be logged per panel
        return Result(ip, "prepare", False, None, str(exc)), None


def set_mode_panel(ip: str, password: str, online: bool) -> Result:
    operation = "online" if online else "offline"
    client = PanelClient(ip, "root", password)
    try:
        status, settings = client.get_json(ASSISTANT_SETTINGS)
        if status != 200:
            return Result(ip, operation, False, status, f"GET настроек: {settings}")
        assistant = settings.setdefault("assistant", {})
        assistant["enable"] = True
        assistant["online"] = online
        put_status, response = client.put_json(ASSISTANT_SETTINGS, settings)
        if put_status != 200:
            return Result(ip, operation, False, put_status, f"PUT настроек: {response}")
        verify_status, verify = client.get_json(ASSISTANT_SETTINGS)
        actual = verify.get("assistant", {}) if verify_status == 200 else {}
        if actual.get("enable") is not True or actual.get("online") is not online:
            return Result(ip, operation, False, verify_status, f"проверка не пройдена: {verify}")
        return Result(ip, operation, True, 200, f"assistant.enable=true, online={str(online).lower()}")
    except Exception as exc:  # noqa: BLE001 - result must be logged per panel
        return Result(ip, operation, False, None, str(exc))


def finish_panel(ip: str, password: str) -> Result:
    """Delete only campaign file 9.wav, then always attempt to restore Online mode."""
    client = PanelClient(ip, "root", password)
    cleanup_ok = True
    cleanup_status: int | None = 200
    cleanup_message = "9.wav already absent"

    try:
        info_status, info = client.get_json(SYSTEM_INFO)
        revision = str(info.get("deviceModel") or info.get("deviceRevision") or "")
        if info_status != 200:
            cleanup_ok = False
            cleanup_status = info_status
            cleanup_message = f"cannot identify panel before cleanup: {info}"
        elif EXPECTED_REVISION.lower() not in revision.lower():
            cleanup_ok = False
            cleanup_status = info_status
            cleanup_message = f"cleanup skipped for unsupported panel {revision!r}"
        else:
            list_status, listing = client.get_json(f"{EVENT_BASE}/list")
            if list_status != 200:
                cleanup_ok = False
                cleanup_status = list_status
                cleanup_message = f"cannot read audio list: {listing}"
            else:
                names = [str(item.get("name")) for item in listing.get("files", []) if item.get("name")]
                if EXPECTED_AUDIO_NAME in names:
                    delete_body = json.dumps([EXPECTED_AUDIO_NAME], separators=(",", ":")).encode("utf-8")
                    delete_status, delete_payload, _ = client.request(
                        "DELETE",
                        f"{EVENT_BASE}/list",
                        body=delete_body,
                        headers={"Content-Type": "application/json"},
                    )
                    cleanup_status = delete_status
                    if delete_status not in (200, 204):
                        cleanup_ok = False
                        cleanup_message = f"cannot delete 9.wav: {decode_error(delete_payload)}"
                    else:
                        verify_status, verify = client.get_json(f"{EVENT_BASE}/list")
                        remaining = [
                            str(item.get("name"))
                            for item in verify.get("files", [])
                            if item.get("name")
                        ] if verify_status == 200 else [EXPECTED_AUDIO_NAME]
                        if EXPECTED_AUDIO_NAME in remaining:
                            cleanup_ok = False
                            cleanup_status = verify_status
                            cleanup_message = f"9.wav is still present: {verify}"
                        else:
                            cleanup_message = "9.wav deleted"
    except Exception as exc:  # noqa: BLE001 - Online restoration must still run
        cleanup_ok = False
        cleanup_status = None
        cleanup_message = f"cleanup error: {exc}"

    online_result = set_mode_panel(ip, password, online=True)
    ok = cleanup_ok and online_result.ok
    status = online_result.status if not online_result.ok else cleanup_status
    return Result(
        ip,
        "finish",
        ok,
        status,
        f"{cleanup_message}; {online_result.message}",
    )


def status_panel(ip: str, password: str) -> Result:
    client = PanelClient(ip, "root", password)
    try:
        settings_status, settings = client.get_json(ASSISTANT_SETTINGS)
        list_status, listing = client.get_json(f"{EVENT_BASE}/list")
        ok = settings_status == 200 and list_status == 200
        return Result(
            ip,
            "status",
            ok,
            settings_status if settings_status != 200 else list_status,
            f"assistant={settings.get('assistant')}; files={listing.get('files')}",
        )
    except Exception as exc:  # noqa: BLE001
        return Result(ip, "status", False, None, str(exc))


def run_parallel(
    ips: list[str],
    operation: str,
    workers: int,
    action: Callable[[str], Result],
) -> list[Result]:
    logging.info("%s: запуск для %d панелей, потоков: %d", operation, len(ips), workers)
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sokol") as executor:
        futures = {executor.submit(action, ip): ip for ip in ips}
        for index, future in enumerate(as_completed(futures), start=1):
            ip = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = Result(ip, operation, False, None, str(exc))
            results.append(result)
            append_result(result)
            level = logging.INFO if result.ok else logging.ERROR
            logging.log(level, "[%d/%d] %s %s — %s", index, len(ips), ip, "OK" if result.ok else "FAIL", result.message)
    succeeded = sum(result.ok for result in results)
    logging.info("%s завершено: успешно %d, ошибок %d", operation, succeeded, len(results) - succeeded)
    return results


def prepare_all(ips: list[str], password: str, workers: int) -> list[Result]:
    if not AUDIO_FILE.exists():
        raise FileNotFoundError(f"не найден файл {AUDIO_FILE.name}")
    audio = AUDIO_FILE.read_bytes()
    state = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "audioFile": EXPECTED_AUDIO_NAME,
        "audioSize": len(audio),
        "panels": {},
    }
    save_state(state)
    state_lock = threading.Lock()

    def action(ip: str) -> Result:
        result, panel_state = prepare_panel(ip, password, audio)
        if result.ok and panel_state is not None:
            with state_lock:
                state["panels"][ip] = panel_state
                save_state(state)
        return result

    return run_parallel(ips, "prepare", workers, action)


def set_mode_all(ips: list[str], password: str, workers: int, online: bool) -> list[Result]:
    operation = "online" if online else "offline"
    return run_parallel(ips, operation, workers, lambda ip: set_mode_panel(ip, password, online))


def finish_all(ips: list[str], password: str, workers: int) -> list[Result]:
    return run_parallel(ips, "finish", workers, lambda ip: finish_panel(ip, password))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Массовая загрузка 9.wav на Сокол Плюс rev.5")
    parser.add_argument(
        "command",
        choices=("test", "prepare", "activate", "offline", "online", "status"),
        nargs="?",
        default="status",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--only", help="выполнить команду только для одного IP")
    parser.add_argument("--password-env", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()
    if not 1 <= args.workers <= 50:
        raise ValueError("--workers должен быть от 1 до 50")
    ips = read_ips()
    if args.only:
        if args.only not in ips:
            logging.warning("IP %s отсутствует в panels.txt, но будет использован для теста", args.only)
        ips = [args.only]
    logging.info("загружено %d уникальных IP из panels.txt", len(ips))
    if args.password_env:
        password = os.environ.get("SOKOL_ROOT_PASSWORD", "")
        if not password:
            logging.error("SOKOL_ROOT_PASSWORD is missing")
            return 1
    else:
        password = getpass.getpass("Введите общий пароль пользователя root: ")
    if not password:
        logging.error("пустой пароль запрещён")
        return 1

    if args.command == "test":
        prepared = prepare_all(ips, password, 1)
        if not all(result.ok for result in prepared):
            return 5
        offline = set_mode_all(ips, password, 1, online=False)
        if not all(result.ok for result in offline):
            set_mode_all(ips, password, 1, online=True)
            return 5
        try:
            input("Панель в Offline. Проверьте открытие ключом и нажмите Enter для возврата в Online...")
        finally:
            finished = finish_all(ips, password, 1)
        return 0 if all(result.ok for result in finished) else 5
    if args.command == "prepare":
        return 0 if all(result.ok for result in prepare_all(ips, password, args.workers)) else 5
    if args.command == "activate":
        state = load_state()
        prepared_ips = [ip for ip in ips if ip in state.get("panels", {})]
        if not prepared_ips:
            logging.error("нет панелей, успешно подготовленных текущим запуском prepare")
            return 6
        logging.info("переключение в Offline только %d подготовленных панелей", len(prepared_ips))
        return 0 if all(result.ok for result in set_mode_all(prepared_ips, password, args.workers, online=False)) else 5
    if args.command == "offline":
        return 0 if all(result.ok for result in set_mode_all(ips, password, args.workers, online=False)) else 5
    if args.command == "online":
        return 0 if all(result.ok for result in finish_all(ips, password, args.workers)) else 5
    results = run_parallel(ips, "status", args.workers, lambda ip: status_panel(ip, password))
    return 0 if all(result.ok for result in results) else 5


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.error("работа остановлена пользователем")
        raise SystemExit(130)
