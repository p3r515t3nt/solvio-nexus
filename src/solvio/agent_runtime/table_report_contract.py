"""First reusable file tool: measured CSV/XLSX analysis and actual report files.

The input grant is the broader offline file boundary. This narrower tool
contract describes what this implementation must actually do, so a different
offline tool is never mistaken for a compatible report writer. Generated code
is built by the existing Autopilot and executes only in FileToolRuntime.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import zipfile

from solvio.agent_runtime import file_inputs as FI, file_tool_process as FP
from solvio.agent_runtime import document_formats as DF

FORMAT = "tables"
CONTRACT = "table_report_v1"
CAPABILITY, VERSION, RESOURCE = FI.CAPABILITY, FI.VERSION, FI.RESOURCE
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_WIRE_BYTES = 12 * 1024 * 1024
MAX_REPORT_TEXT = 10000
CHECKER = Path(__file__).parent / "tool_checks" / "table_report.py"
OUTPUTS = {"Analyse.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "Diagramm.png": "image/png", "Bericht.pdf": "application/pdf"}
CONTRACT_DIGEST = hashlib.sha256(FI._json({
    "contract": CONTRACT, "grant": FI.CONTRACT_DIGEST, "formats": ["csv", "xlsx"],
    "outputs": OUTPUTS, "max_rows": 50000, "max_columns": 128, "max_cells": 500000,
    "max_tables": 8, "source_formulas": "refused_until_recalculated",
    "max_result_bytes": MAX_RESULT_BYTES, "readback": "independent_library_check_v1",
})).hexdigest()


@dataclass(frozen=True)
class TableProfile:
    format: str = FORMAT
    contract: str = CONTRACT
    contract_digest: str = CONTRACT_DIGEST
    capability: str = CAPABILITY
    version: int = VERSION
    resource: str = RESOURCE
    max_input_bytes: int = FP.MAX_INPUT_BYTES


@dataclass(frozen=True)
class BoundTableReport(TableProfile):
    input: FI.BoundFileTask | None = None

    @property
    def task_id(self):
        return self.input.task_id

    @property
    def run_id(self):
        return self.input.run_id

    @property
    def grant_reference(self):
        return self.input.grant_reference

    @property
    def arguments(self):
        return self.input.arguments


def profile():
    return TableProfile()


def for_run(ledger, run_id):
    bound = FI.for_run(ledger, run_id)
    if bound is None:
        return None
    request = FI.read_for_run(ledger, run_id, arguments=bound.arguments)
    if any(not item.name.lower().endswith((".csv", ".xlsx")) for item in request.files):
        raise ValueError("unsupported_table_format")
    return BoundTableReport(input=bound)


def input_payload(request):
    if type(request) is not FI.FileTaskRequest:
        raise ValueError("file_request_required")
    request.__post_init__()
    return FI._json({"version": 1, "operation": "table_report", "files": [
        {"name": item.name, "content_b64": base64.b64encode(item.content).decode("ascii")}
        for item in request.files]})


def invocation(root, entrypoint, files, runtime):
    if type(runtime) is not FP.FileToolRuntime:
        raise ValueError("file_runtime_not_configured")
    return FP.FileToolInvocation(str(root), entrypoint, files, runtime,
        timeout_s=30.0, max_input_bytes=FP.MAX_INPUT_BYTES, max_output_bytes=MAX_WIRE_BYTES)


def environment_fingerprint(runtime):
    if type(runtime) is not FP.FileToolRuntime:
        raise ValueError("file_runtime_not_configured")
    # A configured identity is a catalogue hint. run_file_tool remeasures the
    # actual runtime tree before and after every native execution; do not read
    # hundreds of megabytes synchronously on every catalogue/selection lookup.
    return hashlib.sha256(FI._json({"contract": CONTRACT_DIGEST,
        "runtime": [runtime.python, runtime.prefix, runtime.site_packages, runtime.fingerprint],
        "checker": hashlib.sha256(CHECKER.read_bytes()).hexdigest(),
        "archive_validator": hashlib.sha256(Path(DF.__file__).read_bytes()).hexdigest(),
        "validator": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})).hexdigest()


# A closed wire vocabulary for the independently hashed checker. Never infer a
# category from adapter stderr, library exception text, names or input values.
CHECK_REASONS = frozenset(('active_report', 'active_result_cell', 'active_workbook', 'archive_entry', 'archive_limit', 'archive_stream_invalid', 'cell_limit', 'chart_blank', 'chart_dimensions', 'csv_dialect', 'csv_header', 'empty_sheet', 'encoding', 'external_workbook_relationship', 'nonfinite_cell', 'report_object_limit', 'report_pages', 'report_text_limit', 'report_statistics_missing', 'report_table_missing', 'report_title_missing', 'result_sheet_names', 'sheet_limit', 'source_formulas_require_recalculation', 'statistics_differ_from_source', 'table_limit', 'table_shape', 'unbound_workbook_relationship', 'unsupported_table_format', 'workbook_chart_missing', 'workbook_source_data_changed', 'workbook_statistics_changed', 'workbook_xml_invalid', 'library_parse_failed', 'input_invalid'))
CHECK_STAGES = frozenset({"input", "source", "statistics", "workbook", "chart", "report"})
EXECUTION_STATUSES = frozenset({"terminal", "unknown", "not_started"})


class TableGateFailure(ValueError):
    def __init__(self, reason, *, stage="output", execution_status="terminal"):
        super().__init__(reason)
        self.reason, self.stage, self.execution_status = reason, stage, execution_status


def _status(result):
    return result.execution_status if result.execution_status in EXECUTION_STATUSES else "unknown"


def parse_result(raw):
    if type(raw) is not bytes or len(raw) > MAX_WIRE_BYTES:
        raise TableGateFailure("table_output_limit")
    FP._json_object(raw)
    body = json.loads(raw)
    if (set(body) != {"version", "summary", "tables", "files"}
            or type(body["version"]) is not int or body["version"] != 1
            or type(body["summary"]) is not str or not 1 <= len(body["summary"]) <= 4000
            or "\x00" in body["summary"] or type(body["tables"]) is not list
            or not 1 <= len(body["tables"]) <= 8 or len(FI._json(body["tables"])) > 65536
            or type(body["files"]) is not list or len(body["files"]) != len(OUTPUTS)):
        raise TableGateFailure("table_output_invalid")
    files, size = {}, 0
    for item in body["files"]:
        if (type(item) is not dict or set(item) != {"name", "mime_type", "content_b64"}
                or item.get("name") not in OUTPUTS or item["name"] in files
                or item["mime_type"] != OUTPUTS[item["name"]]
                or type(item["content_b64"]) is not str):
            raise TableGateFailure("table_output_file_invalid")
        data = base64.b64decode(item["content_b64"], validate=True)
        size += len(data)
        if (not data or size > MAX_RESULT_BYTES
                or base64.b64encode(data).decode("ascii") != item["content_b64"]):
            raise TableGateFailure("table_output_file_limit")
        files[item["name"]] = data
    return body, files


def validate_readback(value, *, report_sha256: str):
    """Validate the closed native readback contract, bound to delivered PDF bytes.

    This validates structure and binding, not the truth of arbitrary caller
    claims. validate_output obtains it only from the Core-owned native checker;
    historical callers must separately verify the original durable receipt.
    """
    def keys(item, expected):
        return type(item) is dict and set(item) == expected

    if (type(report_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", report_sha256)
            or not keys(value, {"version", "workbook", "chart", "report"})
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("table_readback_invalid")
    book, chart, report = value["workbook"], value["chart"], value["report"]
    if (not keys(book, {"statistics_match", "source_rows_match", "embedded_chart_count"})
            or book["statistics_match"] is not True or book["source_rows_match"] is not True
            or type(book["embedded_chart_count"]) is not int or book["embedded_chart_count"] < 1
            or not keys(chart, {"format", "width", "height", "nonblank"})
            or type(chart["format"]) is not str or chart["format"] != "PNG"
            or type(chart["width"]) is not int or not 320 <= chart["width"] <= 2400
            or type(chart["height"]) is not int or not 200 <= chart["height"] <= 1600
            or chart["nonblank"] is not True
            or not keys(report, {"sha256", "pages", "text", "text_sha256"})
            or type(report["sha256"]) is not str or report["sha256"] != report_sha256
            or type(report["pages"]) is not int or not 1 <= report["pages"] <= 64
            or type(report["text"]) is not str or not 1 <= len(report["text"]) <= MAX_REPORT_TEXT
            or type(report["text_sha256"]) is not str):
        raise ValueError("table_readback_invalid")
    try:
        text_hash = hashlib.sha256(report["text"].encode("utf-8")).hexdigest()
    except UnicodeError:
        raise ValueError("table_readback_invalid") from None
    if report["text_sha256"] != text_hash:
        raise ValueError("table_readback_invalid")
    return json.loads(FI._json(value))


async def validate_output(call, source, result):
    """Reopen source + generated files in a second, Core-owned sandbox program.

    A successful generated process is not a semantic result. Only facts
    recomputed here can become the task's verified file evidence.
    """
    try:
        body, files = parse_result(result)
    except TableGateFailure:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise TableGateFailure("table_output_invalid") from None
    if type(call) is not FP.FileToolInvocation:
        raise ValueError("file_invocation_required")
    raw = CHECKER.read_bytes()
    check = FP.FileToolInvocation(str(CHECKER.parent.parent), "tool_checks/" + CHECKER.name,
        {"tool_checks/" + CHECKER.name: hashlib.sha256(raw).hexdigest(),
         "document_formats.py": hashlib.sha256(Path(DF.__file__).read_bytes()).hexdigest()}, call.runtime,
        timeout_s=30.0, max_input_bytes=FP.MAX_INPUT_BYTES, max_output_bytes=131072)
    payload = FI._json({"input": json.loads(source), "result": body})
    checked = await FP.run_file_tool(check, payload)
    if not checked.ok or checked.execution_status != "terminal":
        raise TableGateFailure("table_readback_unavailable", stage="checker",
                               execution_status=_status(checked))
    try:
        proof = json.loads(checked.stdout)
    except (ValueError, UnicodeError, RecursionError):
        raise TableGateFailure("table_readback_invalid", stage="checker") from None
    if type(proof) is not dict:
        raise TableGateFailure("table_readback_invalid", stage="checker")
    if (proof.get("ok") is False and set(proof) == {"ok", "reason", "stage"}
            and type(proof["reason"]) is str and proof["reason"] in CHECK_REASONS
            and type(proof["stage"]) is str and proof["stage"] in CHECK_STAGES):
        raise TableGateFailure(proof["reason"], stage=proof["stage"])
    if proof.get("ok") is not True or set(proof) != {"ok", "tables", "readback"}:
        raise TableGateFailure("table_readback_invalid", stage="checker")
    try:
        readback = validate_readback(proof["readback"],
            report_sha256=hashlib.sha256(files["Bericht.pdf"]).hexdigest())
    except ValueError:
        raise TableGateFailure("table_readback_invalid", stage="checker") from None
    # Numeric comparison happens in the checker. Its authoritative facts
    # replace stylistic/native number encoding differences in the adapter.
    return {"summary": body["summary"], "tables": proof["tables"], "files": files, "readback": readback}


def _xlsx_fixture():
    # A deliberately small OOXML test fixture. This is not a product writer.
    content = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Besuche" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B4"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Ort</t></is></c><c r="B1" t="inlineStr"><is><t>Anzahl</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Park</t></is></c><c r="B2"><v>19</v></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Museum</t></is></c><c r="B3"><v>7</v></c></row><row r="4"><c r="A4" t="inlineStr"><is><t>Cafe</t></is></c><c r="B4"><v>11</v></c></row></sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, value in content.items():
            item = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(item, value)
    return buffer.getvalue()


def gate_cases():
    return (FI.FileTaskRequest((FI.FileInput("Umsatz.csv",
                b"Monat,Umsatz,Ausgaben\nJuli,120,37\nAugust,145,41\nSeptember,91,29\n"),)),
        FI.FileTaskRequest((FI.FileInput("Besuche.xlsx", _xlsx_fixture()),
            FI.FileInput("Temperatur.csv", b"Tag;Grad\nMontag;-3.5\nDienstag;0\nMittwoch;7.25\n"))))


async def gate_report(call):
    """One fresh native gate, with bounded Core-only diagnostic categories."""
    cases = gate_cases()
    for index, request in enumerate(cases, 1):
        report = {"version": 1, "ok": False, "case_count": len(cases), "case_index": index,
                  "stage": "adapter", "reason": "adapter_execution_failed", "execution_status": "unknown"}
        source = input_payload(request)
        result = await FP.run_file_tool(call, source)
        if not result.ok or result.execution_status != "terminal":
            report["execution_status"] = _status(result)
            report["reason"] = {"invalid_output_json": "adapter_output_invalid", "timeout": "adapter_timeout",
                "memory_limit": "adapter_memory_limit", "runtime_changed": "runtime_changed"}.get(
                    result.reason, "adapter_execution_failed")
            return report
        try:
            await validate_output(call, source, result.stdout)
        except TableGateFailure as exc:
            report.update(stage=exc.stage, reason=exc.reason, execution_status=exc.execution_status)
            return report
        except (ValueError, OSError):
            report.update(stage="checker", reason="table_readback_unavailable", execution_status="unknown")
            return report
    return {"version": 1, "ok": True, "case_count": len(cases), "case_index": 0,
            "stage": "complete", "reason": "", "execution_status": "terminal"}


async def gate(call):
    return (await gate_report(call))["ok"]


def development_objective():
    return """Build only adapter.py implementing table_report_v1, a reusable offline CSV/XLSX
report tool. Read ONE UTF-8 JSON object on stdin: version=1, operation=table_report,
files=[{name,content_b64}]. Names are labels, NEVER paths. Write ONE JSON object on
stdout: version=1, summary (German, <=4000 chars), tables (facts below), files
[{name,mime_type,content_b64}] exactly Analyse.xlsx, Diagramm.png, Bericht.pdf.
Use installed pandas, openpyxl, xlsxwriter, Pillow, reportlab; all byte buffers
in memory. For xlsxwriter.Workbook, explicitly pass options
{"in_memory":True,"constant_memory":False}. With pandas.ExcelWriter and
engine='xlsxwriter', pass engine_kwargs={"options":{"in_memory":True,
"constant_memory":False}}. This avoids the library's temporary-file path.
TMPDIR does NOT allow creating new files. No install, fork, exec, shell, network, credentials or host reads.
Core executes adapter.py in a separate measured Python runtime and checks output
with an independent, Core-owned native library program. You may not alter it.

Inputs: UTF-8 CSV (measure comma/semicolon with csv.Sniffer, validate ORIGINAL
csv.reader headers before pandas can rename them, then pandas.read_csv using
that separator, engine=python, utf-8-sig; single-column CSV is allowed);
XLSX all sheets, row 1 header, no source formulas. In openpyxl read_only mode
call reset_dimensions and bound ACTUAL iterated rows/cells, never trust declared
worksheet dimension. Reject other types,
formulas, macros, external links/connections/query tables/embedded active objects,
foreign relationship targets, XML DTD/entities and non-UTF-8 OOXML,
duplicate/empty/nonstring headers, >8 tables,
>50000 rows/table, >128 columns, >500000 cells total. Preserve source cells and
missing values, never invent currency or units. Numeric columns are pandas
numeric nonboolean dtype; omit all-missing numeric columns. Fact per table:
{source:filename,sheet:'CSV' or workbook sheet title,rows:count,columns:[headers],
missing:{column:missing_count},numeric:{column:{count,sum,min,max,mean}}}.
Use math.fsum of finite float values; stats omit missing values. Keep source
file/sheet/column order. Unsupported input must exit nonzero, never fake success.

Analyse.xlsx: first sheet Kennzahlen with header exactly
Datei,Tabelle,Spalte,Anzahl,Summe,Minimum,Maximum,Mittelwert, then one row per numeric
column matching facts. Then Daten1..DatenN containing exact original headers and
cells. Store text as text (strings_to_formulas=False, strings_to_urls=False),
no hyperlinks/macros/formulas/external links. Include an actual embedded Excel
chart. Format headers, widths, numbers and freeze top row for readable output.
Diagramm.png: a real labeled bar chart from the data, dimensions 320..2400 by
200..1600, use Pillow (matplotlib is absent). Say what metric/units are known;
if no numeric columns, chart table row counts. PDF: readable German report,
title SOLVIO Tabellenbericht; section marker 'Tabelle N: R Zeilen' per table;
numeric line '<Spaltenname> (Spalte K): Summe: S' (K=1-based ORIGINAL column
position, S=format(sum,'.6g')) inside that table's section. Use the actual column
name; do not expose internal evidence IDs or metric codes. Explain missing values
and that correlations/causes cannot be inferred from this overview. Use reportlab
and BytesIO. The complete extracted PDF text must be <=10000 characters; Core
refuses longer text instead of silently truncating it. Files total <=8MiB. MIME types: XLSX standard spreadsheetml MIME,
image/png, application/pdf. Output no local file paths or execution receipts.
This is a general table overview; it does not claim arbitrary forecasting,
formula recalculation, business conclusions, or completion of the owner task.
Only edit adapter.py in the isolated workspace. Do not create test scripts.
"""
