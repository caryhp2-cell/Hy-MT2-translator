from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
INDEX_HTML = ROOT / "index.html"
BIN_DIR = ROOT / "bin"

# Subprocess tracker
MODEL_PROCESS: subprocess.Popen | None = None


def download_llama_server() -> bool:
    """Downloads llama-server Windows CPU build if not already present."""
    server_path = BIN_DIR / "llama-server.exe"
    if server_path.exists():
        return True

    print("llama-server.exe not found. Starting automatic setup...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    fallback_url = "https://github.com/ggml-org/llama.cpp/releases/download/b9553/llama-b9553-bin-win-cpu-x64.zip"
    zip_path = BIN_DIR / "llama_temp.zip"

    try:
        # Try finding the latest release URL first
        api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        print("Querying latest llama.cpp release...")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            assets = data.get("assets", [])
            download_url = None
            for asset in assets:
                name = asset.get("name", "")
                if "win" in name.lower() and "cpu-x64" in name.lower() and name.endswith(".zip"):
                    download_url = asset.get("browser_download_url")
                    break
            if download_url:
                fallback_url = download_url
    except Exception as e:
        print(f"Note: GitHub API query failed or rate-limited ({e}). Using fallback download URL.")

    print(f"Downloading from: {fallback_url}")
    try:
        # Download the file
        req = urllib.request.Request(fallback_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as response, open(zip_path, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        
        print("Download complete. Extracting files...")
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(BIN_DIR)

        print("Extraction complete. Cleaning up zip file...")
        if zip_path.exists():
            zip_path.unlink()

        if server_path.exists():
            print("llama-server.exe successfully set up in bin/.")
            return True
        else:
            print("Error: llama-server.exe was not found in the extracted zip.")
            return False
    except Exception as e:
        print(f"Failed to setup llama-server.exe: {e}")
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass
        return False


def find_model(models_dir: Path = MODELS_DIR, override_name: str | None = None) -> Path | None:
    if override_name:
        override_path = Path(override_name)
        if override_path.exists():
            return override_path
        local_override = models_dir / override_name
        if local_override.exists():
            return local_override

    models = sorted(models_dir.glob("*.gguf")) if models_dir.exists() else []
    preferred_terms = ("hy-mt", "translation", "translate")
    for model in models:
        name = model.name.lower()
        if any(term in name for term in preferred_terms):
            return model
    return models[0] if models else None


def build_translation_prompt(chinese_text: str) -> str:
    text = chinese_text.strip()
    return (
        "Translate the following segment into English, without additional explanation.\n\n"
        f"{text}"
    )


def clean_translation(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = cleaned.strip()

    labels = (
        "English translation:",
        "Translation:",
        "Final translation:",
        "譯文：",
        "译文：",
    )
    for label in labels:
        index = cleaned.lower().find(label.lower())
        if index >= 0:
            cleaned = cleaned[index + len(label) :].strip()
            break

    unwanted_prefixes = (
        "Thinking Process:",
        "Analyze the Request:",
        "Analyze the Source Text:",
        "Breakdown:",
    )
    lines = [line.strip() for line in cleaned.splitlines()]
    lines = [
        line
        for line in lines
        if line and not any(line.startswith(prefix) for prefix in unwanted_prefixes)
    ]
    return " ".join(lines).strip()


def terminate_llama_process():
    global MODEL_PROCESS
    if MODEL_PROCESS is not None:
        print("Stopping llama-server subprocess...")
        try:
            MODEL_PROCESS.terminate()
            MODEL_PROCESS.wait(timeout=5)
        except Exception:
            try:
                MODEL_PROCESS.kill()
            except Exception:
                pass
        MODEL_PROCESS = None


atexit.register(terminate_llama_process)


class TranslatorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_file(INDEX_HTML, "text/html; charset=utf-8")
            return
        if self.path == "/health":
            self.send_json(
                {
                    "model": str(find_model(override_name=self.server.model_override)) if find_model() else None,
                    "llama_ready": self.server.llama_ready,
                    "llama_port": self.server.llama_port,
                    "llama_error": self.server.llama_error,
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/translate":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            request = json.loads(body)
            chinese_text = str(request.get("text", "")).strip()
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "Invalid JSON request."}, status=400)
            return

        if not chinese_text:
            self.send_json({"error": "Please enter Chinese text first."}, status=400)
            return

        # Ensure server is running
        ready, error = self.server.ensure_llama_server()
        if not ready:
            self.send_json({"error": error}, status=503)
            return

        try:
            translated = self.server.call_llama(chinese_text)
        except urllib.error.URLError as exc:
            self.send_json({"error": f"Translation request failed: {exc}"}, status=502)
            return
        except Exception as exc:
            self.send_json({"error": f"Internal translation failure: {exc}"}, status=500)
            return

        self.send_json({"translation": translated})

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: int = 200) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        # Prevent default logging to keep terminal clean, or log to stderr
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")


class TranslatorServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, args):
        super().__init__(server_address, RequestHandlerClass)
        self.llama_host = args.llama_host
        self.llama_port = args.llama_port
        self.llama_url = f"http://{self.llama_host}:{self.llama_port}"
        self.threads = args.threads
        self.context = args.context
        self.max_tokens = args.max_tokens
        self.model_override = args.model
        self.llama_ready = False
        self.llama_error = None

    def check_llama_health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.llama_url}/health", timeout=1) as response:
                return response.status < 500
        except OSError:
            return False

    def get_loaded_model_path(self) -> Path | None:
        try:
            with urllib.request.urlopen(f"{self.llama_url}/props", timeout=1) as response:
                props = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

        model_path = props.get("model_path")
        return Path(model_path).resolve() if model_path else None

    def ensure_llama_server(self) -> tuple[bool, str | None]:
        global MODEL_PROCESS

        model_path = find_model(override_name=self.model_override)
        if not model_path:
            self.llama_ready = False
            self.llama_error = "No .gguf model found in the models folder."
            return False, self.llama_error
        model_path = model_path.resolve()

        if self.check_llama_health():
            loaded_model = self.get_loaded_model_path()
            if loaded_model and loaded_model != model_path:
                self.llama_ready = False
                self.llama_error = (
                    "llama-server is already running with a different model. "
                    "Please terminate the running llama-server.exe process first."
                )
                return False, self.llama_error
            self.llama_ready = True
            self.llama_error = None
            return True, None

        # Download if missing
        if not download_llama_server():
            self.llama_ready = False
            self.llama_error = "llama-server.exe was not found and automatic setup failed."
            return False, self.llama_error

        server_executable = BIN_DIR / "llama-server.exe"
        if not server_executable.exists():
            server_executable = ROOT / "llama-server.exe"

        command = [
            str(server_executable),
            "-m", str(model_path),
            "--host", self.llama_host,
            "--port", str(self.llama_port),
            "-c", str(self.context),
            "-t", str(self.threads),
            "-tb", str(self.threads),
            "-np", "1",
            "-ngl", "0",
        ]

        print(f"Starting llama-server with: {' '.join(command)}")
        try:
            log_path = ROOT / "llama-server.log"
            log_file = log_path.open("a", encoding="utf-8")
            MODEL_PROCESS = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.llama_ready = False
            self.llama_error = f"Could not start llama-server.exe: {exc}"
            return False, self.llama_error

        # Wait up to 45 seconds for llama-server to boot
        print("Waiting for llama-server to load model...")
        for i in range(45):
            if self.check_llama_health():
                print("llama-server is ready!")
                self.llama_ready = True
                self.llama_error = None
                return True, None
            if MODEL_PROCESS.poll() is not None:
                self.llama_ready = False
                self.llama_error = "llama-server.exe exited early. Please check llama-server.log."
                return False, self.llama_error
            time.sleep(1)

        self.llama_ready = False
        self.llama_error = "Timed out while starting llama-server.exe. Check llama-server.log."
        return False, self.llama_error

    def call_llama(self, chinese_text: str) -> str:
        prompt = build_translation_prompt(chinese_text)
        payload = {
            "model": "local",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "top_p": 0.85,
        }
        request = urllib.request.Request(
            f"{self.llama_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return clean_translation(str(content).strip())


def main() -> None:
    max_cpu = max(1, (os.cpu_count() or 2) - 1)
    default_threads = min(6, max_cpu)

    parser = argparse.ArgumentParser(description="Local Chinese-to-English translator")
    parser.add_argument("--host", default="127.0.0.1", help="Host address for the web server")
    parser.add_argument("--port", default=7860, type=int, help="Port for the web server")
    parser.add_argument("--llama-host", default="127.0.0.1", help="Host address for the llama-server")
    parser.add_argument("--llama-port", default=8081, type=int, help="Port for the llama-server")
    parser.add_argument("--threads", default=default_threads, type=int, help="Threads to use for inference")
    parser.add_argument("--context", default=1024, type=int, help="Context limit for the model")
    parser.add_argument("--max-tokens", default=192, type=int, help="Max tokens generated for output")
    parser.add_argument("--model", default=None, help="Name of specific GGUF model file to load")
    args = parser.parse_args()

    # Pre-check/download llama-server before starting server loop
    download_llama_server()

    server = TranslatorServer((args.host, args.port), TranslatorHandler, args)
    print("=" * 60)
    print(f"Translation UI:     http://{args.host}:{args.port}")
    print(f"Internal Port:      {args.llama_port}")
    print(f"Model File:         {find_model(override_name=args.model) or 'None found (put in models/)'}")
    print(f"Threads:            {args.threads} / Context: {args.context}")
    print("=" * 60)

    # Boot backend asynchronously on first translate, or trigger background warm-up
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        terminate_llama_process()


if __name__ == "__main__":
    main()
