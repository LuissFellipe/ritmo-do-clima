"""Bronze: baixa os ZIPs anuais do INMET só quando mudaram (RF01–RF03)."""

import hashlib
import json
import sys
from datetime import datetime, timezone

import httpx

from ritmo import config

MANIFESTO = config.BRONZE / "manifesto.json"


def ler_manifesto() -> dict:
    if MANIFESTO.exists():
        return json.loads(MANIFESTO.read_text())
    return {}


def gravar_manifesto(manifesto: dict) -> None:
    MANIFESTO.write_text(json.dumps(manifesto, indent=2, ensure_ascii=False, sort_keys=True))


def sha256(caminho) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def baixar_ano(cliente: httpx.Client, ano: int, manifesto: dict) -> bool:
    """Baixa o ZIP do ano se o ETag mudou. Devolve True se baixou."""
    url = config.URL_ZIP.format(ano=ano)
    destino = config.BRONZE / f"{ano}.zip"
    anterior = manifesto.get(str(ano), {})

    cabecalho = cliente.head(url)
    cabecalho.raise_for_status()
    etag = cabecalho.headers.get("etag")
    if destino.exists() and etag and anterior.get("etag") == etag:
        print(f"{ano}: sem mudança (ETag {etag})")
        return False

    temporario = destino.with_suffix(".zip.part")
    with cliente.stream("GET", url) as resposta:
        resposta.raise_for_status()
        with open(temporario, "wb") as f:
            for bloco in resposta.iter_bytes(1 << 20):
                f.write(bloco)
    temporario.replace(destino)

    manifesto[str(ano)] = {
        "url": url,
        "etag": etag,
        "last_modified": cabecalho.headers.get("last-modified"),
        "bytes": destino.stat().st_size,
        "sha256": sha256(destino),
        "baixado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    gravar_manifesto(manifesto)
    print(f"{ano}: baixado ({destino.stat().st_size / 1e6:.0f} MB)")
    return True


def main(anos=None) -> list[int]:
    config.BRONZE.mkdir(parents=True, exist_ok=True)
    anos = anos or range(config.ANO_INICIAL, config.ANO_FINAL + 1)
    manifesto = ler_manifesto()
    baixados = []
    # Um download por vez: o portal é público e não precisa de pressa.
    with httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=120, follow_redirects=True) as cliente:
        for ano in anos:
            if baixar_ano(cliente, ano, manifesto):
                baixados.append(ano)
    return baixados


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or None)
