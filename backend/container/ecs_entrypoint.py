import os
from pathlib import Path

def _run_api():
    auth_content = os.environ.get("NOVAMIND_AUTH_CONTENT")
    auth_mode = os.environ.get("NOVAMIND_AUTH_MODE", "local")
    if auth_mode == "cognito":
        if auth_content:
            raise RuntimeError("NOVAMIND_AUTH_CONTENT is not allowed in Cognito mode")
    elif auth_mode == "local":
        if not auth_content:
            raise RuntimeError("NOVAMIND_AUTH_CONTENT is required for local authentication")
        auth_dir = Path("/dev/shm/novamind")
        auth_dir.mkdir(parents=True, exist_ok=True)
        auth_file = auth_dir / "auth.json"
        auth_file.write_text(auth_content, encoding="utf-8")
        os.environ["NOVAMIND_AUTH_FILE"] = str(auth_file)
    else:
        raise RuntimeError("NOVAMIND_AUTH_MODE is invalid")

    os.execvp(
        "python",
        [
            "python", "-m", "uvicorn",
            "backend.container.bootstrap:create_container_app", "--factory",
            "--host", "0.0.0.0", "--port", "8000", "--workers", "1",
            "--no-proxy-headers", "--no-access-log",
        ],
    )


def main():
    role = os.environ.get("NOVAMIND_PROCESS_ROLE", "api")
    if role in ("api", "local"):
        _run_api()
    if role == "publisher":
        from backend.container.sqs_runtime import run_publisher
        return run_publisher()
    if role == "worker":
        from backend.container.sqs_runtime import run_worker
        return run_worker()
    raise RuntimeError("Container process role is invalid")


if __name__ == "__main__":
    main()
