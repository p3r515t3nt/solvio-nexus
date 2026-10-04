"""Explicit LAN window acceptance preview. Temporary stores, no runtime tick.

Separate HTTPS site and random one-use browser enrollments. No production
certificate, keys, data, provider, Pi, or OS trust-store changes.
"""
import argparse
import asyncio
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import ipaddress
from pathlib import Path
import signal
import ssl
import sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from aiohttp import web

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
from test_nexus_dashboard import world
from solvio.security.mobile_approval import browser_sessions as B

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True);parser.add_argument('--port',type=int,default=8871)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--minutes',type=int,default=20)
    args=parser.parse_args();host=ipaddress.ip_address(args.host)
    if not host.is_private or host.is_unspecified or host.is_multicast:parser.error('specific private interface required')
    if not 1 <= args.minutes <= 60:parser.error('bounded acceptance window: 1 to 60 minutes')
    output=args.output.resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True);output.chmod(0o700)
    async with world() as w:
        key=ec.generate_private_key(ec.SECP256R1());now=datetime.now(timezone.utc)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SOLVIO isolated window acceptance')])
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(host),x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.DNSName('localhost')]),critical=False)
          .sign(key,hashes.SHA256()))
        certfile=Path(w.folder)/'preview-cert.pem';keyfile=Path(w.folder)/'preview-key.pem'
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));keyfile.chmod(0o600)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(certfile,keyfile)
        origin=f'https://{host}:{args.port}'
        w.app[B._ORIGINS]=frozenset({*w.app[B._ORIGINS],origin})
        for device in ('mac','windows'):
            enrollment=await w.sessions.issue_enrollment(principal='local-owner')
            path=output/(device+'-anmeldecode.txt');path.write_text(enrollment.token+'\n');path.chmod(0o600)
        spki=base64.b64encode(hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
        (output/'url.txt').write_text(origin+'/dashboard/')
        (output/'spki.txt').write_text(spki)
        (output/'server-cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        # Public command has only a public-key pin, no enrollment token.
        command='\n'.join([
            "$solvioProfil = Join-Path $env:TEMP 'solvio-n6-window-preview-"+str(int(now.timestamp()))+"'",
            "$solvioAdresse = 'https' + '://"+str(host)+':'+str(args.port)+"/dashboard/'",
            "$solvioArgumente = '--user-data-dir=\"' + $solvioProfil + '\" --ignore-certificate-errors-spki-list="+spki+" ' + $solvioAdresse",
            'Start-Process msedge.exe -ArgumentList $solvioArgumente'])
        (output/'windows-start.ps1').write_text(command+'\n')
        site=web.TCPSite(w.server.runner,str(host),args.port,ssl_context=tls);await site.start()
        print('READY '+origin+'/dashboard/',flush=True)
        stop=asyncio.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):asyncio.get_running_loop().add_signal_handler(sig,stop.set)
        try:
            await asyncio.wait_for(stop.wait(),args.minutes*60)
        except asyncio.TimeoutError:pass
        finally:
            await site.stop()
            print('PREVIEW STOPPED; no provider or production runtime was started',flush=True)

asyncio.run(main())
