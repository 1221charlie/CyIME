
"""Build the offline edition from the app's pinned model catalog."""
import argparse
import hashlib
import json
import re
import shutil
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDED = {
    'ochwpro',
    'paraformer-zh-en-int8',
    'sensevoice-multilingual-int8',
    'speaker-campplus-zh',
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def kotlin_calls(source, function):
    results = []
    for match in re.finditer(r'\b' + re.escape(function) + r'\s*\(', source):
        start = match.end()
        depth, quoted, escaped = 1, False, False
        for end in range(start, len(source)):
            char = source[end]
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    results.append(source[start:end])
                    break
        else:
            raise ValueError(f'Unclosed {function}(...)')
    return results


def split_args(source):
    result, start, depth = [], 0, 0
    quoted, escaped = False, False
    for i, char in enumerate(source):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif char == ',' and depth == 0:
            result.append(source[start:i].strip())
            start = i + 1
    result.append(source[start:].strip())
    return result


def resolve_string(expression, constants):
    match = re.fullmatch(r'"([^"]*)"', expression.strip())
    if not match:
        raise ValueError(f'Unsupported Kotlin string: {expression}')
    return re.sub(
        r'\$(\w+)',
        lambda m: constants.get(m.group(1), m.group(0)),
        match.group(1),
    )


def parse_string_list(expression, constants):
    match = re.search(r'listOf\s*\((.*)\)\s*$', expression, re.S)
    if not match:
        return []
    result = []
    for item in split_args(match.group(1)):
        if not item:
            continue
        joined = re.fullmatch(
            r'([\w.]+)\s*\+\s*(".*")', item, re.S
        )
        if joined:
            prefix = joined.group(1).split('.')[-1]
            if prefix not in constants:
                raise ValueError(f'Unknown mirror constant: {prefix}')
            result.append(
                constants[prefix] +
                resolve_string(joined.group(2), constants)
            )
        else:
            result.append(resolve_string(item, constants))
    return result


def parse_model_files(block, constants):
    files = []
    for args in kotlin_calls(block, 'ModelFile'):
        parts = split_args(args)
        if len(parts) < 4:
            raise ValueError('Invalid ModelFile entry')

        name = resolve_string(parts[0], constants)
        url = resolve_string(parts[1], constants)
        sha = resolve_string(parts[2], constants)
        size_match = re.match(r'(\d+)L?', parts[3])
        if not size_match:
            raise ValueError(f'Invalid file size: {name}')

        mirrors = re.search(
            r'\bmirrors\s*=\s*(listOf\s*\(.*\))', args, re.S
        )
        urls = [url] if url else []
        if mirrors:
            urls.extend(parse_string_list(mirrors.group(1), constants))

        files.append({
            'name': name,
            'urls': list(dict.fromkeys(u for u in urls if u)),
            'sha256': sha,
            'size': int(size_match.group(1)),
        })
    return files


def catalog():
    root = ROOT / 'app/src/main/java/com/kingzcheung/xime'
    mirror_source = (
        root / 'model/DownloadMirrors.kt'
    ).read_text(encoding='utf-8')

    mirror_constants = dict(re.findall(
        r'const val (\w+)\s*=\s*"([^"]+)"',
        mirror_source
    ))

    models = []
    for relative in (
        'model/BuiltinModelCatalog.kt',
        'speech/SpeechModelCatalog.kt',
    ):
        source = (root / relative).read_text(encoding='utf-8')
        constants = dict(re.findall(
            r'const val (\w+)\s*=\s*"([^"]+)"',
            source
        ))
        constants.update(mirror_constants)

        for block in source.split('ModelInfo(')[1:]:
            literal = re.match(r'"([^"]+)"', block)
            symbol = re.match(r'([\w.]+)', block)

            if literal:
                model_id = literal.group(1)
            elif symbol:
                model_id = constants.get(symbol.group(1), '')
            else:
                model_id = ''

            version = re.search(
                r'ModelVersion\s*\(\s*version\s*=\s*"([^"]+)"',
                block
            )
            if not version:
                continue

            first = 'ModelVersion(' + block.split(
                'ModelVersion(', 1
            )[1]
            first = first.split('ModelVersion(', 1)[0]

            title = re.match(
                r'(?:\s*"[^"]+"|\s*[\w.]+),\s*"([^"]+)"',
                block
            )
            files = parse_model_files(first, constants)

            archive_match = re.search(
                r'archiveUrl\s*=\s*"([^"]+)"\s*,\s*sha256\s*=\s*"([^"]+)"',
                first
            )
            archive_mirrors = re.search(
                r'\barchiveMirrors\s*=\s*(listOf\s*\(.*?\))\s*,?\s*files\s*=',
                first,
                re.S
            )

            archive = None
            if archive_match:
                url, sha = archive_match.groups()
                urls = [url]
                if archive_mirrors:
                    urls.extend(parse_string_list(
                        archive_mirrors.group(1), constants
                    ))
                archive = {
                    'urls': list(dict.fromkeys(urls)),
                    'sha256': sha,
                }

            if model_id in INCLUDED:
                if not files:
                    raise ValueError(f'Missing files: {model_id}')
                for item in files:
                    if not item['urls'] or not item['sha256']:
                        raise ValueError(
                            f'Incomplete model: {model_id}/{item["name"]}'
                        )

            models.append({
                'id': model_id,
                'name': title.group(1) if title else model_id,
                'version': version.group(1),
                'files': files,
                'archive': archive,
            })

    ids = [m['id'] for m in models]
    if len(set(ids)) != len(ids) or not INCLUDED.issubset(set(ids)):
        raise ValueError('Model catalog changed; review required')

    return [m for m in models if m['id'] in INCLUDED]


def download(urls, target, sha):
    """Try mirrors in order; accept only the pinned SHA256."""
    if target.is_file() and digest(target) == sha:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.part')
    temporary.unlink(missing_ok=True)
    errors = []

    for index, url in enumerate(urls, 1):
        print(
            f'Downloading {target.name}: source {index}/{len(urls)}',
            flush=True
        )
        try:
            with urllib.request.urlopen(url, timeout=180) as src:
                with temporary.open('wb') as dst:
                    shutil.copyfileobj(src, dst)

            actual = digest(temporary)
            if actual != sha:
                raise ValueError(
                    f'SHA256 mismatch: expected {sha}, got {actual}'
                )

            temporary.replace(target)
            print(f'Verified SHA256: {target.name}', flush=True)
            return

        except Exception as error:
            temporary.unlink(missing_ok=True)
            errors.append(f'{url}: {type(error).__name__}: {error}')
            print(f'Download source failed: {error}', flush=True)

    raise ValueError(
        f'All sources failed for {target.name}: ' + ' | '.join(errors)
    )


def prepare(output, cache, selected=None):
    payload = output / 'bundled-models'
    payload.mkdir(parents=True, exist_ok=True)

    models = [
        m for m in catalog()
        if not selected or m['id'] in selected
    ]

    if selected and {m['id'] for m in models} != set(selected):
        raise ValueError('Unknown model id')

    included = {m['id'] for m in models}
    for stale in payload.iterdir():
        if stale.is_dir() and stale.name not in included:
            if stale.resolve().parent != payload.resolve():
                raise ValueError('Unsafe model path')
            shutil.rmtree(stale)

    for obsolete in ('zipformer-zh-int8',):
        target = (payload / obsolete).resolve()
        if target.parent != payload.resolve():
            raise ValueError('Unsafe obsolete path')
        if target.is_dir():
            shutil.rmtree(target)

    manifest = []

    for model in models:
        directory = payload / model['id']
        directory.mkdir(exist_ok=True)

        archive_path = None
        if model['archive']:
            archive_path = cache / (model['id'] + '.tar.bz2')
            download(
                model['archive']['urls'],
                archive_path,
                model['archive']['sha256']
            )

        for info in model['files']:
            target = directory / info['name']

            if archive_path:
                with tarfile.open(archive_path) as archive:
                    matches = [
                        member for member in archive.getmembers()
                        if member.isfile()
                        and Path(member.name).name == info['name']
                    ]
                    if len(matches) != 1:
                        raise ValueError(
                            f'Archive member missing: {info["name"]}'
                        )
                    with archive.extractfile(matches[0]) as src:
                        with target.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
            else:
                cached = cache / model['id'] / info['name']
                download(info['urls'], cached, info['sha256'])

                if (
                    not target.is_file()
                    or digest(target) != info['sha256']
                ):
                    shutil.copyfile(cached, target)

            if not target.is_file() or not target.stat().st_size:
                raise ValueError(f'Empty model file: {target}')

            if (
                info['size']
                and target.stat().st_size != info['size']
            ):
                raise ValueError(f'Invalid model size: {target}')

            actual = digest(target)
            if actual != info['sha256']:
                raise ValueError(f'Final SHA256 mismatch: {target}')

            info['sha256'] = actual
            info['size'] = target.stat().st_size

        manifest.append({
            'id': model['id'],
            'version': model['version'],
            'files': model['files'],
        })

    (payload / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    total = sum(
        f['size'] for m in manifest for f in m['files']
    )
    print(
        f'Bundled {len(manifest)} models: {total:,} bytes',
        flush=True
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--output',
        type=Path,
        default=ROOT / 'app/build/generated/bundled-model-assets'
    )
    parser.add_argument(
        '--cache',
        type=Path,
        default=ROOT / '.gradle/bundled-model-cache'
    )
    parser.add_argument('--models', default='')
    args = parser.parse_args()

    prepare(
        args.output,
        args.cache,
        [m for m in args.models.split(',') if m]
    )
