import hmac
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response


app = FastAPI(
    title="LaTeX compiler",
    version="1.0.1",
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
    """Safely extract an uploaded ZIP archive."""

    destination_resolved = destination.resolve()

    with zipfile.ZipFile(archive) as zf:

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

        zf.extractall(destination)


async def save_upload(
    upload: UploadFile,
    target: Path,
) -> None:
    """Save an UploadFile while enforcing the upload limit."""

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
) -> subprocess.CompletedProcess[str]:
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
    """Compile one LaTeX document using latexmk."""

    main = (workdir / main_file).resolve()
    workdir_resolved = workdir.resolve()

    if (
        not str(main).startswith(
            str(workdir_resolved) + os.sep
        )
        or not main.is_file()
    ):
        raise HTTPException(
            status_code=400,
            detail="main_file is invalid or missing",
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
) -> Response   
    """Read the PDF into memory before TemporaryDirectory is removed.

    This avoids returning a FileResponse referring to a temporary
    file that no longer exists when Starlette sends the response.
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
    }


@app.post("/compile")
async def compile_project(
    project: UploadFile = File(...),
    main_file: str = Form("main.tex"),
    authorization: str | None = Header(default=None),
):
    """
    Compile a ZIP containing a LaTeX project.

    Multipart fields:

        project    ZIP archive
        main_file  main LaTeX filename

    Returns:

        application/pdf
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

        try:

            safe_extract(
                archive,
                source,
            )

        except zipfile.BadZipFile as exc:

            raise HTTPException(
                status_code=400,
                detail="project must be a ZIP archive",
            ) from exc

        pdf = compile_tex(
            source,
            main_file,
        )

        # IMPORTANT:
        # pdf_response reads the PDF bytes here,
        # while TemporaryDirectory still exists.

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
    Compare two self-contained LaTeX files using latexdiff,
    compile the resulting diff.tex, and return the PDF.
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

        diff_path = workdir / "diff.tex"

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

        diff_path.write_text(
            result.stdout,
            encoding="utf-8",
        )

        pdf = compile_tex(
            workdir,
            "diff.tex",
        )

        # Again, read the result before the temporary
        # directory disappears.

        return pdf_response(
            pdf,
            "latexdiff.pdf",
        )
