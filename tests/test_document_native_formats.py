"""Additive native formats: real local conversion, strict containers and grants.

All data is synthetic and all stores/directories temporary. No provider or
production account is used. The existing RTF contract is checked unchanged.
"""
from __future__ import annotations
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch
import warnings
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import document_contract as DC, document_formats as F, store as S
from solvio.agent_runtime import extension_activation as EA, extension_process as EP
from solvio.agent_runtime.task_start_service import TaskStartService, TaskStepAuthority
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.costs import CostLedger
from solvio.capabilities.document_adapter import TaskDocumentService
import test_agent_task_entry as ENTRY
import test_agent_extension_activation as ACTIVATION

TEXT = 'Grüße aus SOLVIO. Fahrrad zur Werkstatt & danach Café.\n'


def wire(fmt, data):
    return {'operation': 'extract_text', 'format': fmt, 'content_b64': base64.b64encode(data).decode()}


def refused(function, *args):
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError('invalid document accepted')


def rewrite(data, replacements=None, additions=()):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
        for entry in source.infolist():
            value = (replacements or {}).get(entry.filename, source.read(entry))
            if value is not None:
                target.writestr(entry, value)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            for entry, value in additions:
                target.writestr(entry, value)
    return output.getvalue()


def native_input(fmt):
    if fmt == 'txt':
        return TEXT.encode()
    with tempfile.TemporaryDirectory(prefix='solvio-native-format-fixture-') as directory:
        process = subprocess.run(['/usr/bin/textutil', '-format', 'txt', '-convert', fmt,
            '-stdin', '-stdout', '-inputencoding', 'UTF-8'], input=TEXT.encode(),
            cwd=directory, env={'PATH': '/usr/bin:/bin', 'HOME': directory, 'TMPDIR': directory},
            capture_output=True, timeout=10, check=True)
        return process.stdout


def adapter(fmt):
    return 'import os\nos.execv("/usr/bin/textutil", ' + repr(
        ['/usr/bin/textutil', *DC.profile(fmt).native_arguments]) + ')\n'


@contextmanager
def world(fmt, data=None):
    # Reuse the canonical Git publication fixture; its unrelated RTF grant
    # remains untouched. This new format receives its own real task/grant.
    with ACTIVATION.world() as w:
        starts = TaskStartService(w.ledger, grants=w.grants, costs=CostLedger(w.ledger))
        w.task, w.run = starts.create(objective='Lies den beigefuegten Text.',
            scope='research', origin='trusted_dashboard', principal='local-owner',
            receipt=VerifiedTaskReceipt('dashboard_session', 'format:session', 'local-owner'),
            request_id='format-activation-' + fmt,
            document_request=DC.DocumentRequest(data if data is not None else native_input(fmt), fmt))
        require(starts.ready(w.run.run_id))
        w.ledger.set_run_fields(w.run.run_id, development_ref=w.milestone)
        yield w


def t_rtf_digest_descriptor_and_filename_remain_exactly_the_original_contract():
    require_equal(DC.CONTRACT, 'rtf_text_v1')
    require_equal(DC.CONTRACT_DIGEST, 'e50a08fdcbfacda726a3a0759dca7c4ce893fa955fa9080caf22ef85124862e3')
    source = b'{\\rtf1\\ansi Original document.}'
    request = DC.validate_request(wire('rtf', source))
    require_equal(request, DC.DocumentRequest(source))
    require_equal(request.descriptor, {'operation': 'extract_text', 'format': 'rtf',
        'sha256': hashlib.sha256(source).hexdigest(), 'bytes': len(source)})
    require_equal(DC.canonical_request(wire('rtf', source)), wire('rtf', source))
    require_equal((DC.profile('rtf').filename, DC.profile('rtf').max_input_bytes), ('input-document.rtf', 65536))


def t_additive_profiles_have_distinct_fixed_native_arguments_and_digests():
    profiles = [DC.profile(fmt) for fmt in ('rtf', 'txt', 'docx', 'odt')]
    require_equal(len({p.contract_digest for p in profiles}), 4)
    require_equal([p.contract for p in profiles], ['rtf_text_v1','utf8_text_v1','docx_text_v1','odt_text_v1'])
    require_equal([p.max_input_bytes for p in profiles], [65536, 65536, 1048576, 1048576])
    for p in profiles:
        require_equal(p.native_arguments[:2], ('-format', p.format))
        require_equal(p.filename, 'input-document.' + p.format)
    refused(DC.validate_request, wire('pdf', b'%PDF-1.7'))
    refused(DC.validate_request, dict(wire('txt', b'hello'), path='/private/input.txt'))


def t_utf8_text_is_strict_bounded_nonempty_and_preserves_original_bytes():
    for data in [TEXT.encode(), b'\xef\xbb\xbfGruesse', b'x' * 65536]:
        require_equal(DC.validate_request(wire('txt', data)).content, data)
    for data in [b'', b' \n\t', b'\xef\xbb\xbf', b'abc\0def', b'abc\x1b', b'\xff', b'x' * 65537]:
        refused(DC.validate_request, wire('txt', data))
    refused(DC.validate_request, {'operation':'extract_text','format':'txt','content_b64':'YQ==='})


def t_real_native_docx_odt_containers_are_admitted_without_rewriting_bytes():
    for fmt in ('docx','odt'):
        data = native_input(fmt)
        request = DC.validate_request(wire(fmt, data))
        require_equal(request.content, data)
        require_equal(request.format, fmt)
        require_equal(DC.canonical_request(wire(fmt, data)), wire(fmt, data))


def t_truncated_corrupt_and_misdeclared_native_documents_are_refused():
    documents = {fmt: native_input(fmt) for fmt in ('docx','odt')}
    for fmt, data in documents.items():
        for invalid in (data[:-10], b'PK\x03\x04broken', b'not a document',
                        documents['odt' if fmt == 'docx' else 'docx'], data + b'after-zip'):
            refused(DC.validate_request, wire(fmt, invalid))
        changed = bytearray(data)
        # Damage compressed bytes while retaining valid directory metadata.
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            item = next(i for i in archive.infolist() if i.compress_type == zipfile.ZIP_DEFLATED)
        name_length, extra_length = struct.unpack_from('<HH', changed, item.header_offset + 26)
        changed[item.header_offset + 30 + name_length + extra_length + 3] ^= 255
        refused(DC.validate_request, wire(fmt, bytes(changed)))


def t_docx_content_types_main_relationship_and_namespace_are_checked():
    data = F.fixture('docx', 'Read me.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = archive.read('word/document.xml')
        types = archive.read('[Content_Types].xml')
        rels = archive.read('_rels/.rels')
    for replacements in [
        {'word/document.xml': None}, {'[Content_Types].xml': types.replace(F._DOCX.encode(), b'application/bogus')},
        {'word/document.xml': document.replace(F._W.encode(), b'urn:wrong')},
        {'_rels/.rels': rels.replace(b'Target="word/document.xml"', b'Target="http://127.0.0.1/document.xml" TargetMode="External"')},
        {'word/document.xml': document.replace(b'Read me.', b' ')},
    ]:
        refused(DC.validate_request, wire('docx', rewrite(data, replacements)))


def t_odt_mimetype_manifest_main_content_and_empty_document_are_checked():
    data = F.fixture('odt', 'Read me.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest, content = archive.read('META-INF/manifest.xml'), archive.read('content.xml')
    for replacements in [
        {'mimetype': b'application/octet-stream'}, {'META-INF/manifest.xml': None},
        {'META-INF/manifest.xml': manifest.replace(F._ODT.encode(), b'application/bogus')},
        {'META-INF/manifest.xml': manifest.replace(b'full-path="content.xml"', b'full-path="other.xml"')},
        {'META-INF/manifest.xml': manifest.replace(b'</manifest:manifest>', b'<manifest:encryption-data/></manifest:manifest>')},
        {'content.xml': content.replace(F._OFFICE.encode(), b'urn:wrong')},
        {'content.xml': content.replace(b'Read me.', b' ')},
    ]:
        refused(DC.validate_request, wire('odt', rewrite(data, replacements)))
    compressed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as old, zipfile.ZipFile(compressed,'w',compression=zipfile.ZIP_DEFLATED) as new:
        for name in old.namelist():
            new.writestr(name, old.read(name))
    refused(DC.validate_request, wire('odt', compressed.getvalue()))


def t_zip_traversal_duplicate_case_alias_symlink_and_encryption_are_refused():
    data = F.fixture('docx', 'Read me.')
    for name in ('../escape', '/absolute', 'word/../escape', 'word\\escape', 'file:evil', 'word//bad'):
        refused(DC.validate_request, wire('docx', rewrite(data, additions=[(name,b'x')])))
    for name in ('word/document.xml', 'WORD/document.xml'):
        refused(DC.validate_request, wire('docx', rewrite(data, additions=[(name,b'x')])))
    link = zipfile.ZipInfo('word/link');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
    refused(DC.validate_request, wire('docx', rewrite(data, additions=[(link,b'/private/file')])))
    changed=bytearray(data)
    central=changed.index(b'PK\x01\x02');flags=struct.unpack_from('<H',changed,central+8)[0]
    struct.pack_into('<H',changed,central+8,flags|1)
    refused(DC.validate_request, wire('docx',bytes(changed)))


def t_zip_entry_expansion_ratio_and_total_limits_precede_decompression():
    data=F.fixture('docx','Read me.')
    refused(DC.validate_request, wire('docx',rewrite(data, additions=[(f'x{i}',b'x') for i in range(126)])))
    bomb=zipfile.ZipInfo('bomb');bomb.compress_type=zipfile.ZIP_DEFLATED
    refused(DC.validate_request, wire('docx',rewrite(data, additions=[(bomb,b'x'*65536)])))
    # Existing tiny streams advertise oversized central lengths: refuse before
    # touching any archive content, independently of native decompression.
    for mutated_size in (F.MAX_EXPANDED_BYTES+1, 3000000):
        changed=bytearray(data);cursor=0
        for index in range(2):
            central=changed.index(b'PK\x01\x02',cursor)
            struct.pack_into('<I',changed,central+20,mutated_size)
            struct.pack_into('<I',changed,central+24,mutated_size)
            cursor=central+4
        with patch.object(zipfile.ZipFile,'open',side_effect=AssertionError('opened compressed stream before limits')):
            refused(DC.validate_request, wire('docx',bytes(changed)))
    refused(DC.validate_request, wire('docx',b'x'*(F.MAX_INPUT_BYTES+1)))


def t_zip_deflate_cannot_hide_a_large_tail_behind_prefix_length_and_crc():
    source=F.fixture('docx','Small text.')
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as old, zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as new:
        for name in old.namelist():
            data=old.read(name)
            if name=='word/document.xml':
                prefix=data
                data+=b' '*(F.MAX_EXPANDED_BYTES+1)
            new.writestr(name,data)
    changed=bytearray(output.getvalue())
    with zipfile.ZipFile(io.BytesIO(changed)) as archive:
        central=archive.start_dir
    while changed[central:central+4]==b'PK\x01\x02':
        name_length,extra_length,comment_length=struct.unpack_from('<HHH',changed,central+28)
        name=changed[central+46:central+46+name_length]
        if name==b'word/document.xml':
            local=struct.unpack_from('<I',changed,central+42)[0]
            crc=zlib.crc32(prefix)
            for offset,value in ((central+16,crc),(central+24,len(prefix)),(local+14,crc),(local+22,len(prefix))):
                struct.pack_into('<I',changed,offset,value)
            break
        central+=46+name_length+extra_length+comment_length
    else:
        raise AssertionError('native stream repro part missing')
    # Python's ordinary reader accepts the forged prefix, with a valid CRC.
    with zipfile.ZipFile(io.BytesIO(changed)) as archive:
        require_equal(archive.read('word/document.xml'),prefix)
    print('stream-repro: zip_bytes=%d declared_xml=%d actual_xml=%d' % (
        len(changed),len(prefix),len(prefix)+F.MAX_EXPANDED_BYTES+1))
    refused(DC.validate_request,wire('docx',bytes(changed)))


def t_zip_stream_eof_and_no_unused_compressed_tail_are_required():
    source=F.fixture('docx','Small text.')
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        info=archive.getinfo('word/document.xml');expected=archive.read(info);central_start=archive.start_dir
    name_length,extra_length=struct.unpack_from('<HH',source,info.header_offset+26)
    start=info.header_offset+30+name_length+extra_length
    end=start+info.compress_size
    require_equal(end,central_start,'controlled fixture last entry must precede directory')
    compressed=source[start:end]
    extra=zlib.compressobj(wbits=-zlib.MAX_WBITS)
    second=extra.compress(b'ignored second stream')+extra.flush()
    for raw in (compressed+second,compressed+b'extra bytes',compressed[:-1]):
        changed=bytearray(source[:start]+raw+source[end:]);delta=len(raw)-len(compressed)
        struct.pack_into('<I',changed,info.header_offset+18,len(raw))
        central=central_start+delta
        while changed[central:central+4]==b'PK\x01\x02':
            n,e,c=struct.unpack_from('<HHH',changed,central+28)
            if changed[central+46:central+46+n]==b'word/document.xml':
                struct.pack_into('<I',changed,central+20,len(raw));break
            central+=46+n+e+c
        else:
            raise AssertionError('stream fixture entry absent')
        struct.pack_into('<I',changed,len(changed)-22+16,central_start+delta)
        with zipfile.ZipFile(io.BytesIO(changed)) as archive:
            require_equal(archive.read('word/document.xml'),expected,
                          'stdlib accepts the prefix even without exact stream EOF')
        refused(DC.validate_request,wire('docx',bytes(changed)))


def t_valid_zip_data_descriptors_are_supported_but_mismatched_descriptors_are_not():
    class NonSeekable(io.BytesIO):
        def seek(self,*args):
            raise io.UnsupportedOperation('nonseekable synthetic ZIP writer')
    output=NonSeekable()
    with zipfile.ZipFile(io.BytesIO(F.fixture('docx','Read me.'))) as source, \
            zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name,source.read(name))
    data=output.getvalue();F.validate(data,'docx')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info=archive.getinfo('word/document.xml')
    require(info.flag_bits&8)
    n,e=struct.unpack_from('<HH',data,info.header_offset+26)
    descriptor=info.header_offset+30+n+e+info.compress_size
    require_equal(data[descriptor:descriptor+4],b'PK\x07\x08')
    for relative in (4,8,12):
        changed=bytearray(data);changed[descriptor+relative]^=1
        refused(DC.validate_request,wire('docx',bytes(changed)))


def t_xml_entities_external_doctype_invalid_utf8_and_structure_limits_are_refused():
    data=F.fixture('docx','Read me.')
    cases=[b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///private/canary">]><x>&a;</x>',
        b'<!DOCTYPE x SYSTEM "http://127.0.0.1/external"><x/>', b'<x>\xff</x>',
        b'<x>'*65+b'</x>'*65, b'<x>'+b'<a/>'*20000+b'</x>',
        b'<?xml version="1.0" encoding="UTF-16"?><x/>']
    for content in cases:
        refused(DC.validate_request,wire('docx',rewrite(data,additions=[('extra.xml',content)])))
    odt=native_input('odt')
    with zipfile.ZipFile(io.BytesIO(odt)) as archive:
        manifest=archive.read('META-INF/manifest.xml')
    require(b'<!DOCTYPE' in manifest,'actual native legacy declaration exercised')
    for changed in [manifest.replace(b'Manifest.dtd',b'http://127.0.0.1/evil.dtd'),
                    manifest.replace(b'"Manifest.dtd">',b'"Manifest.dtd" [<!ENTITY x "evil">]>')]:
        refused(DC.validate_request,wire('odt',rewrite(odt,{'META-INF/manifest.xml':changed})))


async def t_actual_native_sandbox_reads_each_format_and_core_fixtures_are_deterministic():
    for fmt in ('txt','docx','odt'):
        require_equal(F.gate_cases(fmt),F.gate_cases(fmt))
        with tempfile.TemporaryDirectory(prefix='solvio-format-adapter-') as directory:
            source=adapter(fmt).encode();Path(directory,'adapter.py').write_bytes(source)
            invocation=EP.ExtensionInvocation(os.path.realpath(directory),'adapter.py',
                {'adapter.py':hashlib.sha256(source).hexdigest()},max_input_bytes=DC.profile(fmt).max_input_bytes)
            activation=object.__new__(EA.ExtensionActivation)
            require(await activation._gate_for(invocation,fmt))
            data=native_input(fmt);result=await EP.run_extension(invocation,data)
            require(result.ok,result.reason);require_equal(result.execution_status,'terminal')
            require_equal(result.stdout.decode().strip(),TEXT.strip())


async def t_public_format_admission_binds_one_exact_source_and_survives_fresh_ledger():
    async with ENTRY.world() as w:
        for fmt in ('txt','docx','odt'):
            data=native_input(fmt)
            body=dict(ENTRY.BODY,client_request_id='native-format-'+fmt,document_request=wire(fmt,data))
            responses=await asyncio.gather(w.start(body),w.start(body))
            require_equal([response.status for response in responses],[201,201])
            accepted=[await response.json() for response in responses]
            require_equal(accepted[0]['run_id'],accepted[1]['run_id'])
            run_id=accepted[0]['run_id'];fresh=S.AgentRunLedger(w.ledger.path)
            bound=DC.for_run(fresh,run_id);require_equal(bound.format,fmt)
            require_equal(bound.contract_digest,DC.profile(fmt).contract_digest)
            require_equal(DC.read_for_run(fresh,run_id,arguments=bound.arguments),data)
            source=next(item for item in fresh.artifacts_for_run(run_id) if item.kind=='task_input')
            require_equal(Path(source.path).name,'input-document.'+fmt)
            require_equal(Path(source.path).stat().st_mode&0o777,0o400)
            changed=dict(body,document_request=wire('txt',b'changed exact source'))
            require_equal((await w.start(changed)).status,409)
        require_equal(len(w.ledger.recent_runs()),3)
        require_equal(await w.store.list_pending(),[])


async def t_public_bad_container_and_unknown_format_create_no_task_or_grant():
    async with ENTRY.world() as w:
        for fmt,data in [('docx',native_input('odt')),('odt',native_input('docx')),
                         ('txt',b'\xff'),('pdf',b'%PDF-1.7'),('docx',b'PK\x03\x04broken')]:
            body=dict(ENTRY.BODY,client_request_id='native-format-invalid',document_request=wire(fmt,data))
            require_equal((await w.start(body)).status,400)
        require_equal(w.ledger.recent_runs(),[])
        require_equal(await w.store.list_pending(),[])


async def t_native_activation_is_contract_specific_and_survives_reconstruction():
    for fmt in ('txt','docx','odt'):
        with world(fmt) as w:
            candidate=await w.candidate(adapter(fmt))
            manifest=json.loads(next(Path(a.path).read_text() for a in w.ledger.artifacts_for_run(w.run.run_id)
                                     if a.artifact_id==candidate))
            require_equal(manifest['contract_digest'],DC.profile(fmt).contract_digest)
            require((await w.activation.activate(w.run.run_id,candidate)).ok)
            fresh=EA.ExtensionActivation(S.AgentRunLedger(w.ledger.path),development=w.development,publisher=w.publisher)
            require_equal(fresh.selected(w.run.run_id)[0],candidate)
            bound=DC.for_run(w.ledger,w.run.run_id)
            service=TaskDocumentService(w.ledger,w.activation)
            outcome=await service.execute(bound.arguments,TaskStepAuthority(bound.grant_reference,
                bound.task_id,bound.run_id,'native-format-read'))
            require(outcome.ok,outcome.reason)
            require_equal(outcome.data['text'].strip(),TEXT.strip())
            require_equal(outcome.data['contract_digest'],bound.contract_digest)


async def t_wrong_native_format_adapter_is_red_and_cannot_replace_working_selection():
    with world('docx') as w:
        old=await w.candidate(adapter('docx'));require((await w.activation.activate(w.run.run_id,old)).ok)
        try:
            await w.candidate(adapter('odt'))
        except EA.ActivationRefused as failure:
            require_equal(str(failure),'extension_gate_failed')
        else:
            raise AssertionError('wrong format adapter activated')
        require_equal(w.activation.selected(w.run.run_id)[0],old)


async def t_native_activation_failure_actually_probes_and_restores_prior_version():
    with world('odt') as w:
        old=await w.candidate(adapter('odt'));require((await w.activation.activate(w.run.run_id,old)).ok)
        new=await w.candidate(adapter('odt')+'# replacement\n')
        new_directory=w.activation.candidate(w.run.run_id,new).artifact_dir
        original=EP.run_extension;calls=[]
        async def fail_new(invocation,payload):
            calls.append(invocation.artifact_dir)
            if invocation.artifact_dir==new_directory:
                return EP.ExtensionOutcome(False,reason='nonzero_exit',execution_status='terminal',process_started=True,exit_code=1)
            return await original(invocation,payload)
        with patch.object(EP,'run_extension',fail_new):
            result=await w.activation.activate(w.run.run_id,new)
        require(not result.ok);require(result.rolled_back)
        require_equal(w.activation.selected(w.run.run_id)[0],old)
        require_equal(len(calls),3,'one failed candidate and two real prior-format probes')


async def t_exit_zero_empty_output_cannot_be_a_successful_document_result():
    for fmt in ('txt','docx','odt'):
        with world(fmt) as w:
            cases={source:expected for source,expected in EA.gate_cases(fmt)}
            malicious='import sys\nknown='+repr(cases)+'\nsys.stdout.write(known.get(sys.stdin.buffer.read(),""))\n'
            candidate=await w.candidate(malicious)
            require((await w.activation.activate(w.run.run_id,candidate)).ok)
            bound=DC.for_run(w.ledger,w.run.run_id)
            outcome=await TaskDocumentService(w.ledger,w.activation).execute(bound.arguments,
                TaskStepAuthority(bound.grant_reference,bound.task_id,bound.run_id,'empty-native-read'))
            require(not outcome.ok)
            require_equal(outcome.reason,'document_has_no_text')
            require_equal(outcome.state,'completed')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
