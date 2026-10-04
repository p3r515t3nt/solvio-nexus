"""Build the pinned native renderer in an explicit disposable Hermes checkout.

Requires its upstream npm-ci dependencies and a compatible Node on PATH. Never
installs packages, starts Hermes, or changes the installed Hermes source.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

COMMIT = 'fcbd1076a93841fa88855acce810e342a5b78101'
HERE = Path(__file__).resolve().parent
CORE = HERE.parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    # This opt-in marker is created only by the operator in a disposable archive.
    if not (source/'.solvio-disposable-build').is_file() or (source/'.git').exists():
        parser.error('explicit disposable archive with .solvio-disposable-build required')
    page = source/'web/src/pages/SessionsPage.tsx'
    original = page.read_text().replace('export function MessageList({', 'function MessageList({')
    if hashlib.sha256(original.encode()).hexdigest() != 'a81346b4538397512dc584ee92e43180496c62134231af653c3a36e2f733db54':
        parser.error('unexpected native renderer source')
    if digest(source/'package-lock.json') != '8af2f4e5656497aa504a90bcf5ed287efe77fe56632c73e76316a7c799be6ecb':
        parser.error('unexpected upstream dependency lock')
    page.write_text(original.replace('function MessageList({', 'export function MessageList({', 1))
    for own, target in {'Viewer.tsx':'web/src/SolvioViewer.tsx',
                        'viewer.css':'web/src/solvio-viewer.css',
                        'vite.config.ts':'web/solvio.vite.config.ts',
                        'index.html':'web/solvio-view.html'}.items():
        shutil.copyfile(HERE/own, source/target)
    subprocess.run(['node', '../node_modules/typescript/bin/tsc', '-p', 'tsconfig.app.json', '--noEmit'],
                   cwd=source/'web', check=True)
    subprocess.run(['node', '../node_modules/vite/bin/vite.js', 'build', '--config', 'solvio.vite.config.ts'],
                   cwd=source/'web', check=True)
    output = CORE/'src/solvio/dashboard/assets/hermes'
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(source/'solvio-view-dist', output)
    shutil.copyfile(source/'LICENSE', output/'HERMES-LICENSE.txt')
    manifest = dict(upstream='https://github.com/NousResearch/hermes-agent', commit=COMMIT,
        runtime_version='0.20.5', renderer='web/src/pages/SessionsPage.tsx::MessageList',
        scope='read_only_external_solvio_execution',
        files={str(p.relative_to(output)):digest(p) for p in sorted(output.rglob('*')) if p.is_file()})
    (output/'BUILD.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()
