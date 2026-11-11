import hmac
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="LaTeX compiler", version="1.0.0")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "25000000"))
COMPILE_TIMEOUT = int(os.getenv("COMPILE_TIMEOUT_SECONDS", "120"))
API_KEY = os.getenv("LATEX_API_KEY", "")


def authorise(value: str | None) -> None:
    if not API_KEY:
        raise HTTPException(500, "LATEX_API_KEY is not configured")
    supplied = (value or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(401, "Invalid API key")


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if not str(target).startswith(str(destination.resolve()) + os.sep):
                raise HTTPException(400, "Unsafe path in ZIP archive")
            if member.file_size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "A ZIP member is too large")
        zf.extractall(destination)


async def save_upload(upload: UploadFile, target: Path) -> None:
    total = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Upload is too large")
            handle.write(chunk)


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
            check=False,
            env={**os.environ, "openin_any": "p", "openout_any": "p"},
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408, f"Compilation exceeded {COMPILE_TIMEOUT} seconds") from exc


def compile_tex(workdir: Path, main_file: str) -> Path:
    main = (workdir / main_file).resolve()
    if not str(main).startswith(str(workdir.resolve()) + os.sep) or not main.is_file():
        raise HTTPException(400, "main_file is invalid or missing")
    result = run([
        "latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
        "-file-line-error", "-no-shell-escape", main.name
    ], main.parent)
    pdf = main.with_suffix(".pdf")
    if result.returncode or not pdf.exists():
        log = (result.stdout + "\n" + result.stderr)[-12000:]
        return JSONResponse(status_code=422, content={"error": "Compilation failed", "log": log})
    return pdf


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/compile")
async def compile_project(
    project: UploadFile = File(...),
    main_file: str = Form("main.tex"),
    authorization: str | None = Header(default=None),
):
    authorise(authorization)
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        archive = workdir / "project.zip"
        await save_upload(project, archive)
        source = workdir / "source"
        source.mkdir()
        try:
            safe_extract(archive, source)
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "project must be a ZIP archive") from exc
        output = compile_tex(source, main_file)
        if isinstance(output, JSONResponse):
            return output
        result = workdir / "result.pdf"
        shutil.copy2(output, result)
        return FileResponse(result, media_type="application/pdf", filename="result.pdf")


@app.post("/latexdiff")
async def compile_diff(
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    authorise(authorization)
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        old_path, new_path = workdir / "old.tex", workdir / "new.tex"
        await save_upload(old_file, old_path)
        await save_upload(new_file, new_path)
        diff_path = workdir / "diff.tex"
        result = run(["latexdiff", "old.tex", "new.tex"], workdir)
        if result.returncode:
            return JSONResponse(status_code=422, content={"error": "latexdiff failed", "log": (result.stdout + result.stderr)[-12000:]})
        diff_path.write_text(result.stdout, encoding="utf-8")
        output = compile_tex(workdir, "diff.tex")
        if isinstance(output, JSONResponse):
            return output
        result_pdf = workdir / "latexdiff.pdf"
        shutil.copy2(output, result_pdf)
        return FileResponse(result_pdf, media_type="application/pdf", filename="latexdiff.pdf")
