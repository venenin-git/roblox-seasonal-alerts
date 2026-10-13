import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "items.json"
STATE_PATH = ROOT / "state.json"
API_URL = "https://catalog.roblox.com/v1/catalog/items/details"
THUMBS_URL = {
    "Bundle": "https://thumbnails.roblox.com/v1/bundles/thumbnails",
    "Asset": "https://thumbnails.roblox.com/v1/assets",
}
USER_AGENT = "RobloxSeasonalAlerts/2.0 (availability monitor)"
HEADERS = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
META_KEY = "_meta"
BATCH_SIZE = 100  # a API aceita até 120 itens por consulta
PREVIEW_LIMIT = 5
# O workflow só falha depois de tantas execuções seguidas com erro,
# para não encher o e-mail com falhas passageiras da API.
FAIL_AFTER_CONSECUTIVE_ERRORS = 3

OFF_SALE_VALUES = {"offsale", "offsaledeferred", "notforsale"}
ON_SALE_VALUES = {"sale", "forsale"}
ITEM_URL_RE = re.compile(
    r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(bundles|catalog)/(\d+)", re.IGNORECASE
)
RESTRICTION_LABELS = {
    "limited": "Limited",
    "limitedunique": "Limited U",
    "collectible": "Colecionável",
}
COLOR_AVAILABLE = 0x2ECC71
COLOR_ENDED = 0x95A5A6
COLOR_PREVIEW = 0x3498DB


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "sim"}


def get_webhook_url():
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def get_mention():
    """Texto opcional para marcar alguém no alerta (ex.: <@&ID_DO_CARGO>)."""
    return os.getenv("DISCORD_MENTION", "").strip()


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_state(path=None):
    """Lê o state.json; se estiver corrompido, avisa e recomeça do zero."""
    path = path or STATE_PATH
    try:
        state = load_json(path, {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"AVISO: state.json ilegível ({exc}); começando do zero.", file=sys.stderr)
        return {}
    return state if isinstance(state, dict) else {}


def _field(entry, name):
    """Busca um campo aceitando camelCase e PascalCase."""
    if name in entry:
        return entry[name]
    return entry.get(name[:1].upper() + name[1:])


def _norm(value):
    return str(value).replace(" ", "").replace("_", "").lower()


def _truncate(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def state_key_of(item):
    return f"{item['itemType'].lower()}:{item['id']}"


# --------------------------------------------------------------------------
# Configuração dos itens
# --------------------------------------------------------------------------
def normalize_item(raw):
    """Aceita um link (texto) ou um objeto e devolve um item padronizado.

    Formas aceitas em config/items.json:
      "https://www.roblox.com/bundles/201/Headless-Horseman"
      {"url": "https://www.roblox.com/catalog/123/..."}
      {"itemType": "Bundle", "id": 201, "name": "Opcional", "notify_ended": true}
    """
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"entrada inválida: {raw!r}")

    item_type = raw.get("itemType")
    item_id = raw.get("id")
    url = raw.get("url")

    if url:
        match = ITEM_URL_RE.search(str(url))
        if match:
            item_type = item_type or ("Bundle" if match.group(1).lower() == "bundles" else "Asset")
            item_id = item_id if item_id is not None else match.group(2)
        elif item_id is None:
            raise ValueError(f"link não reconhecido: {url}")

    if item_id is None:
        raise ValueError(f"faltou 'url' ou 'id' em {raw!r}")
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise ValueError(f"id inválido: {item_id!r}")

    item_type = str(item_type or "Asset").strip().capitalize()
    if item_type not in THUMBS_URL:
        raise ValueError(f"itemType deve ser 'Bundle' ou 'Asset', recebido: {item_type!r}")

    path = "bundles" if item_type == "Bundle" else "catalog"
    return {
        "itemType": item_type,
        "id": item_id,
        "name": (str(raw["name"]).strip() if raw.get("name") else None),
        "url": f"https://www.roblox.com/{path}/{item_id}/",
        "notify_ended": bool(raw.get("notify_ended", False)),
    }


def load_items(path=None):
    """Lê e valida a lista. Retorna (itens, erros_de_configuração)."""
    raw_items = load_json(path or CONFIG_PATH, [])
    if not isinstance(raw_items, list):
        return [], ["config/items.json deve ser uma lista"]
    items, errors, seen = [], [], set()
    for raw in raw_items:
        try:
            item = normalize_item(raw)
        except ValueError as exc:
            errors.append(f"config/items.json: {exc}")
            continue
        key = state_key_of(item)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items, errors


# --------------------------------------------------------------------------
# Catálogo Roblox
# --------------------------------------------------------------------------
def post_with_csrf(session, url, payload):
    """POST com o handshake de X-CSRF-TOKEN que a API do Roblox costuma exigir.

    A primeira chamada pode responder 403 com o token no header
    'x-csrf-token'; nesse caso repetimos a chamada com ele.
    """
    response = session.post(url, json=payload, timeout=25)
    if response.status_code == 403 and response.headers.get("x-csrf-token"):
        session.headers["X-CSRF-TOKEN"] = response.headers["x-csrf-token"]
        response = session.post(url, json=payload, timeout=25)
    response.raise_for_status()
    return response


def get_item_details(items, session=None, debug=False):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    result = {}
    for start in range(0, len(items), BATCH_SIZE):
        chunk = items[start : start + BATCH_SIZE]
        payload = {
            "items": [{"itemType": i["itemType"], "id": int(i["id"])} for i in chunk]
        }
        data = post_with_csrf(session, API_URL, payload).json().get("data", [])
        if debug:
            print("DEBUG resposta bruta da API:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        for entry in data:
            if entry.get("id") is not None:
                result[(str(entry.get("itemType", "")).lower(), int(entry["id"]))] = entry
    return result


def get_thumbnail_url(item, attempts=2):
    """Devolve a URL do PNG do item (420x420) ou None se não houver."""
    params = {"size": "420x420", "format": "Png", "isCircular": "false"}
    if item["itemType"] == "Bundle":
        params["bundleIds"] = item["id"]
    else:
        params["assetIds"] = item["id"]
        params["returnPolicy"] = "PlaceHolder"
    for attempt in range(attempts):
        response = requests.get(
            THUMBS_URL[item["itemType"]],
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        if data and data[0].get("imageUrl") and data[0].get("state") == "Completed":
            return data[0]["imageUrl"]
        if attempt + 1 < attempts:
            time.sleep(2)  # miniatura ainda sendo gerada
    return None


def sale_status(entry):
    """Retorna (à_venda, preço).

    Ordem de confiança: booleano explícito > sinais de "fora de venda" >
    sinais de "à venda" > preço positivo (último recurso, só quando a
    resposta não traz nenhum campo de status).
    """
    price = _field(entry, "price")

    explicit = _field(entry, "isForSale")
    if isinstance(explicit, bool):
        return explicit, price

    price_status = _norm(_field(entry, "priceStatus") or "")
    statuses = _field(entry, "itemStatus") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    normalized = {_norm(s) for s in statuses}

    # Sinais de indisponibilidade têm prioridade sobre qualquer preço listado.
    if price_status in OFF_SALE_VALUES or normalized & OFF_SALE_VALUES:
        return False, price

    if price_status == "free":
        return True, price if price is not None else 0
    if normalized & ON_SALE_VALUES:
        return True, price

    has_status_info = bool(price_status or normalized)
    if (
        not has_status_info
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
    ):
        return price > 0, price
    return False, price


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def format_price(price):
    if price is None:
        return "Preço não informado"
    try:
        value = int(price)
    except (TypeError, ValueError):
        return "Preço não informado"
    if value == 0:
        return "Grátis"
    return f"{value:,} Robux".replace(",", ".")


def parse_deadline(value):
    """Converte a data de fim de venda (ISO 8601) em datetime futuro, ou None."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment if moment > datetime.now(timezone.utc) else None


def _fmt_int(value):
    return f"{int(value):,}".replace(",", ".")


def build_embed(item, entry, price, event, image_url=None, preview=False):
    """Monta o embed do Discord. event: 'start' | 'available' | 'ended'."""
    entry = entry or {}
    name = item.get("name") or entry.get("name") or "Item"
    url = item["url"]

    if event == "ended":
        title, color = f"⌛ {name} saiu de venda", COLOR_ENDED
        lead = "O item não está mais disponível para compra no catálogo."
    elif event == "start":
        title, color = f"🔔 {name} já está disponível", COLOR_AVAILABLE
        lead = "Monitoramento iniciado: o item já estava à venda."
    else:
        title, color = f"🎃 {name} está disponível!", COLOR_AVAILABLE
        lead = "Acabou de aparecer à venda no catálogo do Roblox."
    if preview:
        title, color = f"👀 PRÉVIA — {title}", COLOR_PREVIEW

    description = f"{lead}\n[Abrir página oficial]({url})"
    item_text = _field(entry, "description")
    if item_text:
        description += f"\n\n> {_truncate(item_text, 250)}"

    fields = []
    if event != "ended":
        shown_price = format_price(price)
        if price is None and _field(entry, "lowestPrice") is not None:
            shown_price = f"A partir de {format_price(_field(entry, 'lowestPrice'))} (revenda)"
        fields.append({"name": "💰 Preço", "value": shown_price, "inline": True})

    kind = "Pacote" if item["itemType"] == "Bundle" else "Item de catálogo"
    restrictions = _field(entry, "itemRestrictions") or []
    labels = [RESTRICTION_LABELS[_norm(r)] for r in restrictions if _norm(r) in RESTRICTION_LABELS]
    if labels:
        kind += f" • {', '.join(labels)}"
    fields.append({"name": "🏷️ Tipo", "value": kind, "inline": True})

    creator = _field(entry, "creatorName")
    if creator:
        fields.append({"name": "👤 Criador", "value": _truncate(creator, 100), "inline": True})

    if event != "ended":
        deadline = parse_deadline(_field(entry, "offSaleDeadline"))
        if deadline:
            stamp = int(deadline.timestamp())
            fields.append({"name": "⏳ Sai de venda", "value": f"<t:{stamp}:f> (<t:{stamp}:R>)", "inline": False})

        remaining, total = _field(entry, "unitsAvailableForConsumption"), _field(entry, "totalQuantity")
        if isinstance(remaining, int) and isinstance(total, int) and total > 0:
            fields.append({"name": "📦 Estoque", "value": f"{_fmt_int(remaining)} de {_fmt_int(total)} restantes", "inline": True})

        bundled = [b.get("name") for b in (_field(entry, "bundledItems") or []) if isinstance(b, dict) and b.get("name")]
        if bundled:
            lines = [f"• {_truncate(n, 60)}" for n in bundled[:8]]
            if len(bundled) > 8:
                lines.append(f"… e mais {len(bundled) - 8}")
            fields.append({"name": "🧩 Contém", "value": "\n".join(lines)[:1000], "inline": False})

    embed = {
        "title": _truncate(title, 250),
        "url": url,
        "description": description[:2000],
        "color": color,
        "fields": fields,
        "footer": {"text": "Roblox Seasonal Alerts • Confirme preço e disponibilidade na página oficial"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        embed["image"] = {"url": image_url}
    return embed


def post_webhook(payload, max_attempts=3):
    """Envia ao Discord respeitando o limite de requisições (HTTP 429)."""
    webhook_url = get_webhook_url()
    if not webhook_url:
        raise RuntimeError("A variável DISCORD_WEBHOOK_URL não foi configurada.")
    for attempt in range(1, max_attempts + 1):
        response = requests.post(webhook_url, json=payload, timeout=20)
        if response.status_code == 429 and attempt < max_attempts:
            try:
                wait = float(response.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2.0
            time.sleep(min(wait, 10))
            continue
        response.raise_for_status()
        return


def notify(item, entry, price, event, preview=False):
    """Busca a imagem do item e envia o alerta ao Discord."""
    image_url = None
    try:
        image_url = get_thumbnail_url(item)
    except Exception as exc:  # a imagem é um extra: o alerta sai mesmo sem ela
        print(f"AVISO: sem imagem para {item['name'] or item['id']}: {exc}", file=sys.stderr)

    mention = get_mention() if event in {"start", "available"} and not preview else ""
    payload = {
        "username": "Roblox Seasonal Alerts",
        "allowed_mentions": {"parse": ["roles", "users", "everyone"]} if mention else {"parse": []},
        "embeds": [build_embed(item, entry, price, event, image_url, preview)],
    }
    if mention:
        payload["content"] = mention
    post_webhook(payload)


def send_test_message():
    post_webhook({
        "username": "Roblox Seasonal Alerts",
        "allowed_mentions": {"parse": []},
        "content": "✅ Teste do Roblox Seasonal Alerts: o webhook está funcionando.",
    })


# --------------------------------------------------------------------------
# Execução principal
# --------------------------------------------------------------------------
def run_preview(debug=False):
    """Envia alertas de demonstração (sem mexer no estado) para ver o visual."""
    items, config_errors = load_items()
    for error in config_errors:
        print(f"ERRO: {error}", file=sys.stderr)
    if not items:
        print("Nenhum item válido em config/items.json", file=sys.stderr)
        return 1
    try:
        details = get_item_details(items, debug=debug)
    except Exception as exc:
        print(f"Erro consultando o catálogo Roblox: {exc}", file=sys.stderr)
        return 1

    failed = bool(config_errors)
    for item in items[:PREVIEW_LIMIT]:
        entry = details.get((item["itemType"].lower(), item["id"]))
        if entry is None:
            print(f"AVISO: API não retornou {state_key_of(item)}", file=sys.stderr)
            failed = True
            continue
        _, price = sale_status(entry)
        try:
            notify(item, entry, price, "available", preview=True)
            print(f"Prévia enviada: {item['name'] or entry.get('name')}")
        except Exception as exc:
            print(f"Falha ao enviar prévia: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


def run(dry_run=False, debug=False):
    items, config_errors = load_items()
    for error in config_errors:
        print(f"ERRO: {error}", file=sys.stderr)
    if not items:
        print("Nenhum item válido em config/items.json", file=sys.stderr)
        return 1

    previous = load_state()
    meta = dict(previous.get(META_KEY, {}))
    errors = []

    api_ok = True
    try:
        details = get_item_details(items, debug=debug)
    except Exception as exc:
        api_ok = False
        details = {}
        errors.append(f"Erro consultando o catálogo Roblox: {exc}")

    # Mantém só o estado dos itens configurados (itens removidos saem do arquivo).
    configured = {state_key_of(i) for i in items}
    new_state = {k: v for k, v in previous.items() if k in configured}

    for item in items:
        if not api_ok:
            break
        state_key = state_key_of(item)
        entry = details.get((item["itemType"].lower(), item["id"]))
        label = item["name"] or state_key

        if entry is None:
            errors.append(f"API não retornou {label} ({state_key})")
            continue

        is_for_sale, price = sale_status(entry)
        was_for_sale = previous.get(state_key, {}).get("is_for_sale")
        print(f"{label} [{entry.get('name', '?')}]: disponível={is_for_sale}, preço={price}")

        event = None
        if is_for_sale and was_for_sale is None:
            event = "start"
        elif is_for_sale and was_for_sale is False:
            event = "available"
        elif not is_for_sale and was_for_sale is True and item["notify_ended"]:
            event = "ended"

        if event and dry_run:
            print(f"DRY-RUN: enviaria alerta ({event}) para {label}")
        elif event:
            try:
                notify(item, entry, price, event)
            except Exception as exc:
                # Não grava o estado deste item: o alerta será tentado de novo.
                errors.append(f"Falha ao enviar alerta de {label}: {exc}")
                continue

        new_state[state_key] = {
            "is_for_sale": is_for_sale,
            "price": price,
            "name": item["name"] or entry.get("name"),
        }

    # Contador de erros consecutivos (limitado para não gerar commits infinitos).
    if errors:
        meta["consecutive_errors"] = min(
            int(meta.get("consecutive_errors", 0)) + 1, FAIL_AFTER_CONSECUTIVE_ERRORS
        )
    else:
        meta["consecutive_errors"] = 0
    new_state[META_KEY] = meta

    if dry_run:
        print("DRY-RUN: state.json não foi alterado.")
    elif new_state != previous:
        STATE_PATH.write_text(
            json.dumps(new_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    for error in errors:
        print(f"AVISO: {error}", file=sys.stderr)
    if config_errors:
        return 1  # erro de configuração: o usuário precisa corrigir o items.json
    if errors and meta["consecutive_errors"] >= FAIL_AFTER_CONSECUTIVE_ERRORS:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor de itens sazonais do Roblox")
    parser.add_argument("--dry-run", action="store_true", default=env_flag("DRY_RUN"),
                        help="consulta a API, mas não envia alertas nem grava state.json")
    parser.add_argument("--debug", action="store_true", default=env_flag("DEBUG"),
                        help="imprime a resposta bruta da API")
    parser.add_argument("--test-webhook", action="store_true", default=env_flag("TEST_WEBHOOK"),
                        help="envia uma mensagem de teste ao Discord e sai")
    parser.add_argument("--preview", action="store_true", default=env_flag("PREVIEW"),
                        help="envia um alerta de demonstração por item (não altera o estado)")
    args = parser.parse_args(argv)

    if args.test_webhook:
        try:
            send_test_message()
        except Exception as exc:
            print(f"Falha ao enviar mensagem de teste: {exc}", file=sys.stderr)
            return 1
        print("Mensagem de teste enviada ao Discord.")
        return 0

    if args.preview:
        return run_preview(debug=args.debug)

    return run(dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
