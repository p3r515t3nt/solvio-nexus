"""Independent native table gates: actual files, actual source recomputation.

The producer is explicit test code. Native pandas/openpyxl/Pillow/pypdf read
the resulting bytes in the bounded private runtime. No providers or accounts.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import xml.etree.ElementTree as XML
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from _table_report_adapter import SOURCE
from test_agent_file_tool_process import runtime
from solvio.agent_runtime import table_report_contract as TC, file_inputs as FI
from solvio.agent_runtime import file_tool_process as FP, document_formats as DF


@contextmanager
def invocation(source=SOURCE):
    with tempfile.TemporaryDirectory(prefix='table-report-producer-') as folder:
        root = Path(folder).resolve()
        raw = source.encode()
        (root / 'adapter.py').write_bytes(raw)
        (root / 'adapter.py').chmod(0o400)
        yield TC.invocation(str(root), 'adapter.py', {'adapter.py': hashlib.sha256(raw).hexdigest()}, runtime())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


async def produce(call, request=None):
    payload = TC.input_payload(request or TC.gate_cases()[0])
    result = await FP.run_file_tool(call, payload)
    require(result.ok, result.reason)
    require_equal(result.execution_status, 'terminal')
    return payload, result.stdout


async def rejected(call, source, output):
    try:
        await TC.validate_output(call, source, output)
    except (ValueError, OSError):
        return
    raise AssertionError('changed or unproven source/output was accepted')


def file_bytes(body, name):
    return base64.b64decode(next(f for f in body['files'] if f['name'] == name)['content_b64'])


def with_file(body, name, raw):
    item = next(f for f in body['files'] if f['name'] == name)
    item['content_b64'] = base64.b64encode(raw).decode('ascii')
    return encoded(body)


def rewrite_zip(raw, name, transform):
    target = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(target, 'w') as new:
        for entry in old.infolist():
            data = old.read(entry)
            new.writestr(entry, transform(data) if entry.filename == name else data)
    return target.getvalue()


async def t_actual_csv_xlsx_reports_pass_both_independent_core_gates():
    with invocation() as call:
        require(await TC.gate(call), 'actual source/output library gate rejected test producer')
        source, raw = await produce(call)
        checked = await TC.validate_output(call, source, raw)
        require_equal(checked['tables'][0]['numeric']['Umsatz']['sum'], 356.0)
        require_equal(set(checked['files']), {'Analyse.xlsx', 'Diagramm.png', 'Bericht.pdf'})
        require(checked['files']['Diagramm.png'].startswith(b'\x89PNG'))
        require(checked['files']['Bericht.pdf'].startswith(b'%PDF-'))
        readback = checked['readback']
        require_equal(readback['version'], 1)
        require_equal(readback['workbook'], {'statistics_match':True,'source_rows_match':True,'embedded_chart_count':1})
        require_equal(readback['chart'], {'format':'PNG','width':960,'height':540,'nonblank':True})
        report = readback['report']
        require_equal(report['pages'], 1)
        require_equal(report['sha256'], hashlib.sha256(checked['files']['Bericht.pdf']).hexdigest())
        require_equal(report['text_sha256'], hashlib.sha256(report['text'].encode('utf-8')).hexdigest())
        for marker in ('SOLVIO Tabellenbericht', '(Spalte 2): Summe: 356', '(Spalte 3): Summe: 107',
                       'Die Uebersicht erlaubt keine Schlussfolgerung zu Korrelationen oder Ursachen.'):
            require(marker in report['text'], 'actual PDF content missing from readback: ' + marker)


async def t_statistics_plus_or_minus_one_are_rejected_against_actual_source():
    with invocation() as call:
        source, raw = await produce(call)
        for difference in (-1, 1):
            body = json.loads(raw)
            body['tables'][0]['numeric']['Umsatz']['sum'] += difference
            await rejected(call, source, encoded(body))


async def t_workbook_cannot_substitute_another_source_cell():
    with invocation() as call:
        source, raw = await produce(call)
        body = json.loads(raw)
        def alter(data):
            root = XML.fromstring(data)
            value = root.find('.//{*}c[@r="B2"]/{*}v')
            require(value is not None)
            value.text = '121'
            return XML.tostring(root, encoding='utf-8', xml_declaration=True)
        changed = rewrite_zip(file_bytes(body, 'Analyse.xlsx'), 'xl/worksheets/sheet2.xml', alter)
        await rejected(call, source, with_file(body, 'Analyse.xlsx', changed))


async def t_missing_pdf_unknown_output_and_blank_png_do_not_form_a_result():
    with invocation() as call:
        source, raw = await produce(call)
        body = json.loads(raw)
        body['files'] = [f for f in body['files'] if f['name'] != 'Bericht.pdf']
        await rejected(call, source, encoded(body))
        body = json.loads(raw)
        body['files'][0]['name'] = 'foreign-private-file.txt'
        await rejected(call, source, encoded(body))
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 960, 540, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff' * (960 * 3)) * 540)) + chunk(b'IEND', b'')
        await rejected(call, source, with_file(json.loads(raw), 'Diagramm.png', png))


async def t_ambiguous_original_csv_headers_cannot_hide_behind_pandas_renaming():
    legacy = SOURCE.replace(
        'if not header or len(set(header)) != len(header) or any(not value.strip() for value in header):',
        'if False:  # emulate a producer accepting pandas header repair')
    require(legacy != SOURCE)
    with invocation(legacy) as call:
        # Produce internally consistent reports from pandas' repaired headers.
        # The old Core checker accepted these; original-header validation must
        # reject even when facts/workbook/PNG/PDF all agree with that repair.
        for csv in (b'Umsatz,Umsatz\n1,2\n3,4\n', b',Umsatz\n1,2\n3,4\n'):
            request = FI.FileTaskRequest((FI.FileInput('ambiguous.csv', csv),))
            source, raw = await produce(call, request)
            await rejected(call, source, raw)


async def t_xlsx_declared_dimension_cannot_truncate_actual_rows():
    with invocation() as call:
        xlsx = TC._xlsx_fixture()
        def undersize(data):
            root = XML.fromstring(data)
            root.find('{*}dimension').set('ref', 'A1:B2')
            return XML.tostring(root, encoding='utf-8', xml_declaration=True)
        xlsx = rewrite_zip(xlsx, 'xl/worksheets/sheet1.xml', undersize)
        request = FI.FileTaskRequest((FI.FileInput('Besuche.xlsx', xlsx),))
        source, raw = await produce(call, request)
        checked = await TC.validate_output(call, source, raw)
        require_equal(checked['tables'][0]['rows'], 3)
        require_equal(checked['tables'][0]['numeric']['Anzahl']['sum'], 37.0)


async def t_page_actions_and_names_attachments_are_rejected_by_real_pdf_graph():
    # The original producer is held fixed; each extra test-only mutation uses
    # actual pypdf objects and writes a valid readable PDF, not broken bytes.
    prefix = SOURCE.rsplit('\nif __name__ == "__main__":', 1)[0]
    for mutation in ('page_action', 'attachment'):
        tail = '''
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject
request = json.load(sys.stdin)
answer = report(read_tables(request['files']))
pdf_file = next(f for f in answer['files'] if f['name']=='Bericht.pdf')
writer = PdfWriter()
writer.clone_document_from_reader(PdfReader(io.BytesIO(base64.b64decode(pdf_file['content_b64']))))
'''
        tail += ("writer.pages[0][NameObject('/AA')] = DictionaryObject({NameObject('/O'): DictionaryObject({NameObject('/S'): NameObject('/JavaScript'), NameObject('/JS'): TextStringObject('app.alert(1)')})})\n"
                 if mutation == 'page_action' else "writer.add_attachment('hidden.txt', b'synthetic hidden data')\n")
        tail += "output=io.BytesIO()\nwriter.write(output)\npdf_file['content_b64']=base64.b64encode(output.getvalue()).decode('ascii')\nprint(json.dumps(answer,ensure_ascii=False,allow_nan=False))\n"
        with invocation(prefix + tail) as call:
            source, raw = await produce(call)
            await rejected(call, source, raw)


async def t_single_column_text_and_nonlatin_column_positions_remain_supported():
    for filename, data in (('Namen.csv', 'Name\nPark\nMuseum\n'),
                           ('Werte.csv', '地域,値\nA,2\nB,3\n')):
        with invocation() as call:
            source, raw = await produce(call, FI.FileTaskRequest((FI.FileInput(filename, data.encode()),)))
            checked = await TC.validate_output(call, source, raw)
            require_equal(checked['tables'][0]['rows'], 2)
            require_equal(checked['tables'][0]['columns'], data.splitlines()[0].split(','))


async def t_actual_zip_stream_rejects_declared_prefix_with_matching_prefix_crc():
    prefix = b'A' * 30
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('payload.bin', prefix + b'X' * (4 * 1024 * 1024))
    mutated = bytearray(zipped.getvalue())
    central = mutated.index(b'PK\x01\x02')
    for crc_at, size_at in ((14, 22), (central + 16, central + 24)):
        struct.pack_into('<I', mutated, crc_at, zlib.crc32(prefix))
        struct.pack_into('<I', mutated, size_at, len(prefix))
    # Demonstrate the concrete standard-library prefix behavior first.
    with zipfile.ZipFile(io.BytesIO(mutated)) as archive:
        require_equal(archive.read('payload.bin'), prefix)
    with tempfile.TemporaryDirectory(prefix='table-archive-counterexample-') as folder:
        root = Path(folder).resolve()
        (root / 'tool_checks').mkdir()
        sources = {'document_formats.py': Path(DF.__file__).read_bytes(),
            'tool_checks/table_report.py': TC.CHECKER.read_bytes(),
            'probe.py': b'''import base64,json,runpy
from pathlib import Path
functions=runpy.run_path(str(Path(__file__).parent/'tool_checks/table_report.py'))
payload=json.load(__import__('sys').stdin)
try:
    functions['archive'](base64.b64decode(payload['bytes']))
except ValueError:
    print('{"rejected":true}')
else:
    print('{"rejected":false}')
'''}
        for name, content in sources.items():
            (root / name).write_bytes(content)
        call = FP.FileToolInvocation(str(root), 'probe.py',
            {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}, runtime())
        result = await FP.run_file_tool(call, encoded({'bytes': base64.b64encode(mutated).decode()}))
        require(result.ok, result.reason)
        require_equal(json.loads(result.stdout), {'rejected': True})


async def t_workbook_connections_and_foreign_relationships_are_refused():
    with invocation() as call:
        source, raw = await produce(call)
        for active_kind in ('connection', 'external_relationship'):
            body = json.loads(raw)
            target = io.BytesIO()
            with (zipfile.ZipFile(io.BytesIO(file_bytes(body, 'Analyse.xlsx'))) as old,
                    zipfile.ZipFile(target, 'w') as new):
                for entry in old.infolist():
                    data = old.read(entry)
                    if entry.filename == 'xl/_rels/workbook.xml.rels':
                        tree = XML.fromstring(data)
                        attrs = {'Id': 'rIdSyntheticExternal',
                            'Type': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/connections',
                            'Target': 'connections.xml'}
                        if active_kind == 'external_relationship':
                            attrs.update(Target='https://synthetic.example.invalid/book.xlsx', TargetMode='External')
                        XML.SubElement(tree, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship', attrs)
                        data = XML.tostring(tree, encoding='utf-8', xml_declaration=True)
                    new.writestr(entry, data)
                if active_kind == 'connection':
                    new.writestr('xl/connections.xml',
                        '<connections xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                        '<connection id="1" name="Synthetic external connection" type="5" refreshOnLoad="1">'
                        '<dbPr connection="ODBC;DSN=synthetic" command="SELECT 1"/>'
                        '</connection></connections>')
            await rejected(call, source, with_file(body, 'Analyse.xlsx', target.getvalue()))


async def t_gate_report_identifies_second_case_and_core_semantic_stage_without_data():
    # The first native fixture succeeds; only the second output lies about a
    # source statistic. Its existing files are otherwise left untouched.
    prefix = SOURCE.rsplit('\nif __name__ == "__main__":', 1)[0]
    tail = """
request = json.load(sys.stdin)
answer = report(read_tables(request['files']))
if len(request['files']) == 2:
    answer['tables'][0]['numeric']['Anzahl']['sum'] += 1
print(json.dumps(answer, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
"""
    with invocation(prefix + tail) as call:
        detail = await TC.gate_report(call)
        require_equal(detail, {'version':1,'ok':False,'case_count':2,'case_index':2,
            'stage':'statistics','reason':'statistics_differ_from_source','execution_status':'terminal'})
        for secret in ('Besuche', 'Anzahl', str(call.artifact_dir), 'content_b64'):
            require(secret not in json.dumps(detail))


async def t_adapter_crash_timeout_and_not_started_keep_distinct_fixed_diagnostics():
    secret = 'PRIVATE-DO-NOT-REPORT /private/owner/report.csv'
    with invocation('raise RuntimeError(' + repr(secret) + ')\n') as call:
        detail = await TC.gate_report(call)
        require_equal(detail['stage'], 'adapter')
        require_equal(detail['case_index'], 1)
        require_equal(detail['reason'], 'adapter_execution_failed')
        require_equal(detail['execution_status'], 'terminal')
        require(secret not in json.dumps(detail))
        missing = await TC.gate_report(replace(call, entrypoint='missing.py'))
        require_equal(missing['execution_status'], 'not_started')
        require_equal(missing['ok'], False)
    with invocation('while True: pass\n') as call:
        detail = await TC.gate_report(replace(call, timeout_s=0.05))
        require_equal(detail['execution_status'], 'unknown')
        require_equal(detail['reason'], 'adapter_timeout')
        require_equal(detail['ok'], False)


async def t_invalid_pdf_is_generic_library_failure_and_never_raw_exception_text():
    with invocation() as call:
        source, raw = await produce(call)
        output = with_file(json.loads(raw), 'Bericht.pdf', b'PRIVATE-FILENAME /owner/secret.csv')
        try:
            await TC.validate_output(call, source, output)
        except TC.TableGateFailure as exc:
            require_equal(exc.reason, 'library_parse_failed')
            require_equal(exc.stage, 'report')
            require_equal(exc.execution_status, 'terminal')
            require('PRIVATE' not in str(exc) and '/owner' not in str(exc))
        else:
            raise AssertionError('malformed PDF was accepted')


def t_library_valueerror_cannot_impersonate_a_core_semantic_failure():
    # Test the actual fixed checker boundary with an injected parser failure;
    # libraries may throw arbitrary ValueError text, even a known Core label.
    import runpy
    original = list(sys.path)
    try:
        namespace = runpy.run_path(str(TC.CHECKER))
    finally:
        sys.path[:] = original
    check = namespace['check']
    scope = check.__globals__
    def library(payload, progress):
        progress[0] = 'report'
        raise ValueError('statistics_differ_from_source')
    scope['_check'] = library
    require_equal(check({}), {'ok':False,'reason':'library_parse_failed','stage':'report'})
    def own(payload, progress):
        progress[0] = 'statistics'
        raise scope['CheckRefused']('statistics_differ_from_source')
    scope['_check'] = own
    require_equal(check({}), {'ok':False,'reason':'statistics_differ_from_source','stage':'statistics'})


async def t_complete_multipage_pdf_text_reaches_readback_without_prefix_truncation():
    # Retain a tail well beyond old summary/snippet sizes on later PDF pages.
    extra = '    for index in range(75):\n        line("Anhang %03d: Diese Zusatzzeile muss vollstaendig im Inhaltsbeleg bleiben." % index)\n    line("ENDE-DES-VOLLSTAENDIGEN-ANHANGS")\n    pdf.save()'
    source_code = SOURCE.replace('    pdf.save()', extra)
    require(source_code != SOURCE)
    with invocation(source_code) as call:
        source, raw = await produce(call)
        checked = await TC.validate_output(call, source, raw)
        report = checked['readback']['report']
        require(report['pages'] >= 2)
        require(1000 < len(report['text']) < TC.MAX_REPORT_TEXT)
        require('Anhang 000:' in report['text'])
        require('Anhang 074:' in report['text'])
        require(report['text'].rstrip().endswith('ENDE-DES-VOLLSTAENDIGEN-ANHANGS'))
        require_equal(report['text_sha256'], hashlib.sha256(report['text'].encode('utf-8')).hexdigest())


async def t_oversized_extracted_pdf_text_is_refused_instead_of_cut_off():
    extra = '    for index in range(175):\n        line("Anhang %03d: Diese Zusatzzeile muss vollstaendig im Inhaltsbeleg bleiben." % index)\n    pdf.save()'
    source_code = SOURCE.replace('    pdf.save()', extra)
    require(source_code != SOURCE)
    with invocation(source_code) as call:
        source, raw = await produce(call)
        try:
            await TC.validate_output(call, source, raw)
        except TC.TableGateFailure as exc:
            require_equal(exc.reason, 'report_text_limit')
            require_equal(exc.stage, 'report')
            require_equal(exc.execution_status, 'terminal')
        else:
            raise AssertionError('oversized report was accepted or silently shortened')


def t_readback_schema_binds_exact_pdf_and_full_text_without_extra_quality_claims():
    import copy
    pdf_hash = 'a' * 64
    def value(text='Vollstaendiger Bericht.\n'):
        return {'version':1,'workbook':{'statistics_match':True,'source_rows_match':True,'embedded_chart_count':1},
            'chart':{'format':'PNG','width':960,'height':540,'nonblank':True},
            'report':{'sha256':pdf_hash,'pages':1,'text':text,
                'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()}}
    original = value()
    verified = TC.validate_readback(original, report_sha256=pdf_hash)
    require_equal(verified, original)
    require(verified is not original and verified['report'] is not original['report'])
    require_equal(TC.validate_readback(value('ä' * 10000), report_sha256=pdf_hash)['report']['text'], 'ä' * 10000)
    cases = []
    for path, replacement in ((('version',),True), (('version',),2),
        (('workbook','statistics_match'),1), (('workbook','source_rows_match'),False),
        (('workbook','embedded_chart_count'),True), (('workbook','embedded_chart_count'),0),
        (('chart','format'),'JPEG'), (('chart','width'),319), (('chart','height'),1601),
        (('chart','nonblank'),False), (('report','pages'),True), (('report','pages'),65),
        (('report','sha256'),'b'*64), (('report','text'),'changed'), (('report','text_sha256'),'c'*64)):
        candidate = copy.deepcopy(original)
        target = candidate if len(path)==1 else candidate[path[0]]
        target[path[-1]] = replacement
        cases.append(candidate)
    cases += [value('ä' * 10001), value('')]
    for section, name in ((None,'readable'), ('workbook','chart_data_correct'), ('chart','data_match'), ('report','understandable')):
        candidate = copy.deepcopy(original)
        (candidate if section is None else candidate[section])[name] = True
        cases.append(candidate)
    candidate = copy.deepcopy(original);del candidate['report']['pages'];cases.append(candidate)
    for candidate in cases:
        try:
            TC.validate_readback(candidate, report_sha256=pdf_hash)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid, unbound or exaggerated readback was accepted')
    try:
        TC.validate_readback(original, report_sha256='b'*64)
    except ValueError:
        pass
    else:
        raise AssertionError('different delivered PDF accepted the old content proof')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
