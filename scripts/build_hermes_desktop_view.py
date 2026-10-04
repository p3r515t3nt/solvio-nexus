#!/usr/bin/env python3
"""Build the pinned Desktop reading components without booting Desktop.

Dependencies must already be installed with the upstream lockfile in an
isolated checkout. This command never installs packages or starts a gateway.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess

PIN = 'fcbd1076a93841fa88855acce810e342a5b78101'
ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-tree', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    args = parser.parse_args()
    tree = args.hermes_tree.resolve()
    commit = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != PIN or subprocess.check_output(['git', '-C', str(tree), 'diff', 'HEAD', '--name-only'], text=True).strip():
        raise SystemExit('Requires the exact clean tracked Hermes source at ' + PIN)
    source = ROOT / 'scripts/hermes-desktop-view'
    adapter_files = {p.name: digest(p) for p in sorted(source.iterdir()) if p.is_file()}
    overlay = tree / 'apps/desktop/solvio'
    overlay.mkdir(exist_ok=True)
    for path in source.iterdir():
        if path.suffix in {'.tsx', '.ts', '.html', '.css'}:
            shutil.copyfile(path, overlay / path.name)
    vite = tree / 'node_modules/vite/bin/vite.js'
    subprocess.run([str(args.node), str(vite), 'build', '--config', str(overlay / 'vite.config.ts')], cwd=tree, check=True)
    output = tree / 'solvio-desktop-view-dist'
    projection = json.loads((output / 'SOURCE.json').read_text())
    expected = ['apps/desktop/src/components/assistant-ui/thread/list.tsx',
                'apps/desktop/src/components/assistant-ui/thread/system-message.tsx',
                'apps/desktop/src/components/chat/scaffold-row.tsx',
                'apps/desktop/src/components/ui/tool-icon.tsx']
    if not all(name in projection['sources'] for name in expected):
        raise SystemExit('Required original Desktop components did not enter the built renderer')
    for name, expected_digest in projection['sources'].items():
        if digest(tree / name) != expected_digest:
            raise SystemExit('Source changed while building: ' + name)
    if adapter_files != {p.name: digest(p) for p in sorted(source.iterdir()) if p.is_file()}:
        raise SystemExit('Core adapter changed while building')
    for name, expected_digest in adapter_files.items():
        if Path(name).suffix in {'.tsx', '.ts', '.html', '.css'} and digest(overlay / name) != expected_digest:
            raise SystemExit('Copied build adapter differs from Core source: ' + name)
    shutil.copyfile(tree / 'LICENSE', output / 'LICENSE-Hermes.txt')
    notices = []
    for folder, package in sorted(projection['packages'].items()):
        notices.append(f"{package['name']} {package['version']} — {package.get('license', 'see package')}\n")
        for license_file in package['licenses']:
            notices.append((tree / folder / license_file).read_text(errors='replace'))
    # Font-only packages do not necessarily occur in JS chunks.
    for filename in ('node_modules/katex/LICENSE', 'node_modules/@vscode/codicons/LICENSE',
                     'node_modules/@nous-research/ui/LICENSE', 'apps/desktop/src/fonts/OFL.txt'):
        if (tree / filename).is_file():
            notices.extend([filename, (tree / filename).read_text(errors='replace')])
    # The public distribution uses a system font where redistribution terms
    # for the upstream Collapse asset have not been established.
    for css in output.rglob('*.css'):
        style = css.read_text()
        style = re.sub(r'@font-face\s*\{[^{}]*Collapse[^{}]*\}', '', style)
        style = style.replace('\"Collapse\"', 'system-ui').replace("'Collapse'", 'system-ui')
        css.write_text(style)
    for font in output.rglob('Collapse*.woff2'):
        font.unlink()
    ofl = ROOT / 'src/solvio/dashboard/assets/hermes/LICENSE-JetBrainsMono-OFL.txt'
    if not ofl.is_file():
        raise SystemExit('The public font license notice is required')
    shutil.copyfile(ofl, output / ofl.name)
    notices.extend([ofl.name, ofl.read_text()])
    (output / 'THIRD-PARTY-LICENSES.txt').write_text('\n\n'.join(notices))
    target = ROOT / 'src/solvio/dashboard/assets/hermes'
    # Replace only the renderer package, never the independent Core shell.
    shutil.rmtree(target)
    shutil.copytree(output, target)
    manifest = {
        'upstream': 'https://github.com/NousResearch/hermes-agent', 'commit': PIN,
        'runtime_version': '0.20.5', 'desktop_version': '0.17.0',
        'renderer': 'apps/desktop/src/components/assistant-ui/thread/list.tsx::ThreadMessageList',
        'scope': 'read_only_external_solvio_execution',
        'components': expected, 'core_adapter': 'scripts/hermes-desktop-view',
        'lock_sha256': digest(tree / 'package-lock.json'),
        'adapter_files': adapter_files,
        'build_script_sha256': digest(Path(__file__).resolve()),
        'node_version': subprocess.check_output([str(args.node), '--version'], text=True).strip(),
        'execution_transport_modules': projection['execution_transport_modules'],
        'files': {str(p.relative_to(target)): digest(p) for p in sorted(target.rglob('*')) if p.is_file()},
    }
    (target / 'BUILD.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'renderer': manifest['renderer'], 'files': len(manifest['files']),
                      'source_files': len(projection['sources']), 'execution_transport_modules': 0}))


if __name__ == '__main__':
    main()
