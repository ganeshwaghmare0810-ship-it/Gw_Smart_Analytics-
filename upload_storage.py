"""Private, validated dataset upload storage shared by the Flask entry points."""

import csv
import hashlib
from io import BytesIO, StringIO
import json
import mimetypes
import os
import shutil
import sqlite3
import tarfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from werkzeug.utils import secure_filename

import database


ALLOWED_EXTENSIONS = {
    "csv", "tsv", "txt", "xlsx", "xls", "xml", "json", "jsonl", "ndjson",
    "parquet", "db", "sqlite", "sqlite3", "sql", "npy", "npz", "arff",
    "zip", "tar", "gz", "7z", "pdf", "docx", "pptx", "jpg", "jpeg", "png", "gif",
}
ALLOWED_MIME_TYPES = {
    "csv": {"text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"},
    "tsv": {"text/tab-separated-values", "text/plain", "application/octet-stream"},
    "txt": {"text/plain", "application/octet-stream"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip", "application/octet-stream"},
    "xls": {"application/vnd.ms-excel", "application/octet-stream"},
    "xml": {"application/xml", "text/xml", "text/plain", "application/octet-stream"},
    "json": {"application/json", "text/json", "text/plain", "application/octet-stream"},
    "jsonl": {"application/json", "text/plain", "application/octet-stream"},
    "ndjson": {"application/x-ndjson", "application/json", "text/plain", "application/octet-stream"},
    "parquet": {"application/vnd.apache.parquet", "application/octet-stream"},
    "db": {"application/x-sqlite3", "application/vnd.sqlite3", "application/octet-stream"},
    "sqlite": {"application/x-sqlite3", "application/vnd.sqlite3", "application/octet-stream"},
    "sqlite3": {"application/x-sqlite3", "application/vnd.sqlite3", "application/octet-stream"},
    "sql": {"application/sql", "text/x-sql", "text/plain", "application/octet-stream"},
    "npy": {"application/x-npy", "application/octet-stream"},
    "npz": {"application/x-npz", "application/zip", "application/octet-stream"},
    "arff": {"text/plain", "application/octet-stream"},
    "zip": {"application/zip", "application/x-zip-compressed", "application/octet-stream"},
    "tar": {"application/x-tar", "application/octet-stream"},
    "gz": {"application/gzip", "application/x-gzip", "application/octet-stream"},
    "7z": {"application/x-7z-compressed", "application/octet-stream"},
    "pdf": {"application/pdf", "application/octet-stream"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip", "application/octet-stream"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip", "application/octet-stream"},
    "jpg": {"image/jpeg", "application/octet-stream"},
    "jpeg": {"image/jpeg", "application/octet-stream"},
    "png": {"image/png", "application/octet-stream"},
    "gif": {"image/gif", "application/octet-stream"},
}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("The uploaded file is not valid text.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


_TABULAR_EXTENSIONS = {
    "csv", "tsv", "txt", "xlsx", "xls", "xml", "json", "jsonl", "ndjson",
    "parquet", "db", "sqlite", "sqlite3", "sql", "npy", "npz", "arff",
}


def _extension(filename: str) -> str:
    return Path(str(filename)).suffix.lower().lstrip(".")


def _sql_connection(raw: bytes) -> sqlite3.Connection:
    """Load a restricted SQLite dump in memory; never permit arbitrary SQL operations."""
    try:
        script = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("SQL dump must be UTF-8 encoded.") from error
    connection = sqlite3.connect(":memory:")
    allowed_actions = {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_TRANSACTION,
    }
    for action_name in ("SQLITE_SAVEPOINT", "SQLITE_CREATE_INDEX"):
        action = getattr(sqlite3, action_name, None)
        if action is not None:
            allowed_actions.add(action)

    def authorize(action, _arg1, _arg2, _database, _trigger):
        if action == getattr(sqlite3, "SQLITE_PRAGMA", None):
            return sqlite3.SQLITE_OK if (
                str(_arg1).lower() == "foreign_keys" and str(_arg2).lower() in {"0", "1", "off", "on"}
            ) else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_UPDATE and str(_arg1).lower() in {"sqlite_master", "sqlite_schema"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if action in allowed_actions else sqlite3.SQLITE_DENY

    deadline = time.monotonic() + 5
    connection.set_authorizer(authorize)
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.executescript(script)
    except sqlite3.DatabaseError as error:
        connection.close()
        raise ValueError(
            "SQL dump contains unsupported operations or exceeded the processing time limit. "
            "Only CREATE TABLE, INSERT, read operations, and the foreign_keys pragma are allowed."
        ) from error
    connection.set_progress_handler(None, 0)
    connection.set_authorizer(None)
    return connection


def _sqlite_connection(extension: str, raw: bytes) -> sqlite3.Connection:
    if extension == "sql":
        return _sql_connection(raw)
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(raw)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (sqlite3.DatabaseError, AttributeError) as error:
        connection.close()
        raise ValueError("Could not read the SQLite database file.") from error


def _sqlite_tables(connection: sqlite3.Connection) -> List[str]:
    tables = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [name for name, create_sql in tables if not str(create_sql or "").upper().startswith("CREATE VIRTUAL TABLE")]


def dataset_options(filename: str, raw: bytes) -> List[str]:
    """Return selectable table/array names for multi-table or multi-array files."""
    extension = _extension(filename)
    if extension in {"db", "sqlite", "sqlite3", "sql"}:
        connection = _sqlite_connection(extension, raw)
        try:
            return _sqlite_tables(connection)
        finally:
            connection.close()
    if extension == "npz":
        import numpy as np
        try:
            with np.load(BytesIO(raw), allow_pickle=False) as archive:
                return list(archive.files)
        except (ValueError, OSError) as error:
            raise ValueError(f"Could not read the NumPy archive: {error}") from error
    return []


def _array_frame(array, name: str):
    import numpy as np
    import pandas as pd
    values = np.asarray(array)
    if values.dtype.hasobject:
        raise ValueError("Object arrays are not supported; save the data using a non-object NumPy dtype.")
    if values.dtype.names:
        return pd.DataFrame.from_records(values)
    if values.ndim == 0:
        return pd.DataFrame({name: [values.item()]})
    if values.ndim == 1:
        return pd.DataFrame({name: values})
    if values.ndim == 2:
        return pd.DataFrame(values)
    flattened = values.reshape(values.shape[0], -1)
    return pd.DataFrame(flattened)


def read_dataset(filename: str, raw: bytes, selection: Optional[str] = None):
    """Read supported, non-executable tabular formats into a pandas DataFrame."""
    import numpy as np
    import pandas as pd

    extension = _extension(filename)
    if extension not in _TABULAR_EXTENSIONS:
        if extension in {"pkl", "pickle"}:
            raise ValueError("Pickle files are not accepted because loading them can execute arbitrary code. Export as CSV or Parquet.")
        raise ValueError(f"Unsupported data format: .{extension or '(no extension)'}")
    bio = BytesIO(raw)
    if extension in {"csv", "tsv", "txt"}:
        if extension == "csv":
            try:
                return pd.read_csv(bio)
            except UnicodeDecodeError:
                bio.seek(0)
                return pd.read_csv(bio, encoding="latin-1")
        if extension == "tsv":
            return pd.read_csv(bio, sep="\t")
        sample = raw[:4096].decode("utf-8-sig", errors="replace")
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = ","
        return pd.read_csv(bio, sep=delimiter)
    if extension in {"xlsx", "xls"}:
        try:
            return pd.read_excel(bio)
        except ImportError as error:
            package = "xlrd" if extension == "xls" else "openpyxl"
            raise ValueError(f"Install `{package}` to read .{extension} files.") from error
    if extension == "json":
        value = json.loads(raw.decode("utf-8-sig"))
        if isinstance(value, dict):
            for key in ("data", "records", "results", "rows"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if isinstance(value, list):
            return pd.json_normalize(value)
        if isinstance(value, dict):
            return pd.json_normalize(value)
        raise ValueError("JSON must contain an object or an array of records.")
    if extension in {"jsonl", "ndjson"}:
        return pd.read_json(bio, lines=True)
    if extension == "xml":
        frame = pd.read_xml(bio, parser="etree")
        if frame is None:
            raise ValueError("XML file did not contain a readable table.")
        return frame
    if extension == "parquet":
        try:
            return pd.read_parquet(bio)
        except ImportError as error:
            raise ValueError("Install `pyarrow` (or `fastparquet`) to read Parquet files.") from error
    if extension in {"db", "sqlite", "sqlite3", "sql"}:
        connection = _sqlite_connection(extension, raw)
        try:
            tables = _sqlite_tables(connection)
            if not tables:
                raise ValueError("No readable tables were found in this database.")
            if selection not in tables:
                if len(tables) != 1:
                    raise ValueError("Select a table to load: " + ", ".join(tables))
                selection = tables[0]
            quoted_table = '"' + selection.replace('"', '""') + '"'
            try:
                return pd.read_sql_query(f"SELECT * FROM {quoted_table}", connection)
            except sqlite3.DatabaseError as error:
                raise ValueError(f"Could not read table `{selection}`: {error}") from error
        finally:
            connection.close()
    if extension in {"npy", "npz"}:
        try:
            if extension == "npy":
                array = np.load(bio, allow_pickle=False)
                return _array_frame(array, Path(filename).stem)
            with np.load(bio, allow_pickle=False) as archive:
                options = list(archive.files)
                if not options:
                    raise ValueError("NumPy archive contains no arrays.")
                if selection not in options:
                    if len(options) != 1:
                        raise ValueError("Select an array to load: " + ", ".join(options))
                    selection = options[0]
                return _array_frame(archive[selection], selection)
        except (ValueError, OSError) as error:
            if "Select an array" in str(error) or "contains no arrays" in str(error):
                raise
            raise ValueError(f"Could not read the NumPy file: {error}") from error
    if extension == "arff":
        try:
            from scipy.io import arff
            records, _metadata = arff.loadarff(StringIO(raw.decode("utf-8-sig")))
            frame = pd.DataFrame(records)
            for column in frame.select_dtypes(include=["object"]).columns:
                frame[column] = frame[column].map(
                    lambda value: value.decode("utf-8") if isinstance(value, bytes) else value
                )
            return frame
        except ImportError as error:
            raise ValueError("Install `scipy` to read ARFF files.") from error
    raise ValueError(f"Unsupported data format: .{extension}")


def _inspect_file(path: Path, extension: str) -> Tuple[str, Optional[int], Optional[int], Optional[List[str]]]:
    """Validate file signatures and extract basic tabular metadata when supported."""
    with path.open("rb") as stream:
        header = stream.read(16)
    if extension in {"xlsx", "docx", "pptx", "zip"} and not zipfile.is_zipfile(path):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "xls" and not header.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "pdf" and not header.startswith(b"%PDF-"):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "png" and not header.startswith(bytes.fromhex("89504E470D0A1A0A")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension in {"jpg", "jpeg"} and not header.startswith(bytes.fromhex("FFD8FF")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "gif" and not header.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "gz" and not header.startswith(bytes.fromhex("1F8B")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "7z" and not header.startswith(bytes.fromhex("377ABCAF271C")):
        raise ValueError("The uploaded file does not match its file extension.")
    if extension == "tar" and not tarfile.is_tarfile(path):
        raise ValueError("The uploaded file does not match its file extension.")

    if extension in {"csv", "tsv", "txt"}:
        text = _read_text(path)
        delimiter = "\t" if extension == "tsv" else ","
        reader = csv.reader(text.splitlines(), delimiter=delimiter)
        header_row = next(reader, [])
        row_count = sum(1 for _ in reader)
        return "READY", row_count, len(header_row), header_row
    if extension == "json":
        value = json.loads(_read_text(path))
        rows = value if isinstance(value, list) else value.get("data", [value]) if isinstance(value, dict) else [value]
        columns = sorted({str(key) for row in rows if isinstance(row, dict) for key in row})
        return "READY", len(rows), len(columns), columns
    if extension in {"jsonl", "ndjson"}:
        rows = [json.loads(line) for line in _read_text(path).splitlines() if line.strip()]
        columns = sorted({str(key) for row in rows if isinstance(row, dict) for key in row})
        return "READY", len(rows), len(columns), columns
    if extension == "xml":
        root = ET.parse(path).getroot()
        names = sorted({child.tag for child in root})
        return "READY", len(root), len(names), names
    if extension in {"xlsx", "xls"}:
        try:
            import pandas as pd
            frame = pd.read_excel(path)
            columns = [str(column) for column in frame.columns]
            return "READY", int(len(frame.index)), len(columns), columns
        except ImportError:
            return "STORED", None, None, None
        except Exception as error:
            raise ValueError(f"Could not read the spreadsheet: {error}") from error
    if extension in {"parquet", "db", "sqlite", "sqlite3", "sql", "npy", "npz", "arff"}:
        raw = path.read_bytes()
        options = dataset_options(path.name, raw)
        frame = read_dataset(path.name, raw, options[0] if options else None)
        columns = [str(column) for column in frame.columns]
        return "READY", int(len(frame.index)), len(columns), columns
    if extension == "zip" and not zipfile.is_zipfile(path):
        raise ValueError("The uploaded file is not a valid ZIP archive.")
    return "STORED", None, None, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_dataset_upload(file: Any, user_id: int) -> Dict[str, Any]:
    """Validates, stores, and records an upload under a random server-side key."""
    raw_name = str(getattr(file, "filename", "") or "")
    basename = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    safe_name = secure_filename(basename)
    if not safe_name or "." not in safe_name:
        raise ValueError("Please choose a valid file name.")
    extension = safe_name.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("This file type is not supported.")
    supplied_mime = str(getattr(file, "mimetype", "") or "").lower()
    if supplied_mime and supplied_mime not in ALLOWED_MIME_TYPES[extension]:
        raise ValueError("The uploaded file's MIME type does not match its extension.")

    database.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_name = f"{uuid.uuid4().hex}.{extension}"
    final_path = database.UPLOAD_DIR / saved_name
    temporary_path = database.UPLOAD_DIR / f".{uuid.uuid4().hex}.uploading"
    try:
        file.save(temporary_path)
        file_size = temporary_path.stat().st_size
        max_size = int(os.getenv("GW_MAX_UPLOAD_MB", "50")) * 1024 * 1024
        if file_size <= 0:
            raise ValueError("The uploaded file is empty.")
        if file_size > max_size:
            raise ValueError(f"File too large (maximum {max_size // (1024 * 1024)} MB).")

        processing_status, row_count, column_count, column_names = _inspect_file(temporary_path, extension)
        checksum = _sha256(temporary_path)
        with temporary_path.open("rb") as source, final_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        temporary_path.unlink()
        upload_id = database.log_upload(
            original_name=safe_name,
            saved_name=saved_name,
            file_size=file_size,
            user_id=user_id,
            mime_type=(mimetypes.guess_type(safe_name)[0] or "application/octet-stream"),
            processing_status=processing_status,
            row_count=row_count,
            column_count=column_count,
            column_names=column_names,
            checksum=checksum,
        )
        return {
            "id": upload_id,
            "original_name": safe_name,
            "saved_name": saved_name,
            "size_bytes": file_size,
            "mime_type": (mimetypes.guess_type(safe_name)[0] or "application/octet-stream"),
            "processing_status": processing_status,
            "row_count": row_count,
            "column_count": column_count,
            "column_names": column_names,
            "checksum": checksum,
        }
    except Exception:
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
