import hmac
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response


app = FastAPI(
    title="LaTeX compiler",
    version="1.1.0",
)


MAX_UPLOAD_BYTES = int(
    os.getenv("MAX_UPLOAD_BYTES", "25000000")
)

COMPILE_TIMEOUT = int(
    os.getenv("COMPILE_TIMEOUT_SECONDS", "120")
)

API_KEY = os.getenv("LATEX_API_KEY", "")


def authorise(value: str | None) -> None:
    """Check the bearer token supplied by the client."""

    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="LATEX_API_KEY is not configured",
        )

    supplied = (value or "").removeprefix("Bearer ").strip()

    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
        )


def safe_extract(
    archive: Path,
    destination: Path,
) -> None:
    """Safely extract a ZIP archive."""

    destination_resolved = destination.resolve()

    try:
        with zipfile.ZipFile(archive) as zf:

            total_uncompressed = 0

            for member in zf.infolist():

                target = (
                    destination / member.filename
                ).resolve()

                if not str(target).startswith(
                    str(destination_resolved) + os.sep
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Unsafe path in ZIP archive",
                    )

                if member.file_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="A ZIP member is too large",
                    )

                total_uncompressed += member.file_size

                if total_uncompressed > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Uncompressed ZIP contents are too large",
                    )

            zf.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid ZIP archive",
        ) from exc


async def save_upload(
    upload: UploadFile,
    target: Path,
) -> None:
    """Save an uploaded file with a size limit."""

    total = 0

    with target.open("wb") as handle:

        while chunk := await upload.read(1024 * 1024):

            total += len(chunk)

            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Upload is too large",
                )

            handle.write(chunk)


def run(
    command: list[str],
    cwd: Path,
) -> subprocess.CompletedProcess:
    """Run a compiler command with a timeout."""

    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
            check=False,
            env={
                **os.environ,
                "openin_any": "p",
                "openout_any": "p",
            },
        )

    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=408,
            detail=(
                f"Compilation exceeded "
                f"{COMPILE_TIMEOUT} seconds"
            ),
        ) from exc


def compile_tex(
    workdir: Path,
    main_file: str,
) -> Path:
    """Compile a LaTeX document using latexmk."""

    main = (workdir / main_file).resolve()
    workdir_resolved = workdir.resolve()

    if not str(main).startswith(
        str(workdir_resolved) + os.sep
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid main file path",
        )

    if not main.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Main LaTeX file not found: {main_file}",
        )

    if main.suffix.lower() != ".tex":
        raise HTTPException(
            status_code=400,
            detail="Main file must be a .tex file",
        )

    result = run(
        [
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            "-no-shell-escape",
            main.name,
        ],
        main.parent,
    )

    pdf = main.with_suffix(".pdf")

    if result.returncode != 0 or not pdf.exists():

        log = (
            result.stdout
            + "\n"
            + result.stderr
        )[-12000:]

        raise HTTPException(
            status_code=422,
            detail={
                "error": "Compilation failed",
                "log": log,
            },
        )

    return pdf


def pdf_response(
    pdf: Path,
    filename: str,
) -> Response:
    """
    Read the PDF before the temporary directory is removed.
    """

    pdf_bytes = pdf.read_bytes()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )


@app.get("/health")
def health():
    """Health-check endpoint."""

    return {
        "status": "ok",
        "service": "latex-compiler",
        "version": "1.1.0",
    }


@app.post("/compile")
async def compile_project(
    project: UploadFile = File(...),
    main_file: str = Form("main.tex"),
    authorization: str | None = Header(default=None),
):
    """
    Compile a LaTeX project supplied as multipart/form-data.

    Fields:
        project    ZIP archive
        main_file  main .tex filename

    This endpoint is useful with curl and conventional HTTP clients.
    """

    authorise(authorization)

    with tempfile.TemporaryDirectory() as temporary:

        workdir = Path(temporary)

        archive = workdir / "project.zip"

        await save_upload(
            project,
            archive,
        )

        source = workdir / "source"
        source.mkdir()

        safe_extract(
            archive,
            source,
        )

        pdf = compile_tex(
            source,
            main_file,
        )

        return pdf_response(
            pdf,
            "result.pdf",
        )


@app.post("/compile-zip")
async def compile_zip(
    request: Request,
    authorization: str | None = Header(default=None),
    x_main_file: str = Header(
        default="main.tex",
        alias="X-Main-File",
    ),
):
    """
    Power Automate-friendly compilation endpoint.

    Request:
        Content-Type: application/zip
        Authorization: Bearer ...
        X-Main-File: main.tex

    Body:
        Raw ZIP data

    Response:
        Raw PDF data.
    """

    authorise(authorization)

    content_length = request.headers.get("content-length")

    if content_length is not None:

        try:
            declared_size = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid Content-Length",
            )

        if declared_size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Upload is too large",
            )

    body = await request.body()

    if not body:
        raise HTTPException(
            status_code=400,
            detail="Request body is empty",
        )

    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Upload is too large",
        )

    with tempfile.TemporaryDirectory() as temporary:

        workdir = Path(temporary)

        archive = workdir / "project.zip"

        archive.write_bytes(body)

        source = workdir / "source"
        source.mkdir()

        safe_extract(
            archive,
            source,
        )

        pdf = compile_tex(
            source,
            x_main_file,
        )

        return pdf_response(
            pdf,
            "result.pdf",
        )


@app.post("/latexdiff")
async def compile_diff(
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """
    Compare two self-contained .tex files using latexdiff
    and return the compiled comparison as a PDF.
    """

    authorise(authorization)

    with tempfile.TemporaryDirectory() as temporary:

        workdir = Path(temporary)

        old_path = workdir / "old.tex"
        new_path = workdir / "new.tex"

        await save_upload(
            old_file,
            old_path,
        )

        await save_upload(
            new_file,
            new_path,
        )

        result = run(
            [
                "latexdiff",
                "old.tex",
                "new.tex",
            ],
            workdir,
        )

        if result.returncode != 0:

            log = (
                result.stdout
                + "\n"
                + result.stderr
            )[-12000:]

            raise HTTPException(
                status_code=422,
                detail={
                    "error": "latexdiff failed",
                    "log": log,
                },
            )

        diff_path = workdir / "diff.tex"

        diff_path.write_text(
            result.stdout,
            encoding="utf-8",
        )

        pdf = compile_tex(
            workdir,
            "diff.tex",
        )

        return pdf_response(
            pdf,
            "latexdiff.pdf",
        )
