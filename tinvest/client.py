"""Минимальный REST-клиент T-Invest API, только для чтения.

Торговые методы (PostOrder, CancelOrder, стоп-заявки и т.п.) сюда намеренно
не пускаются: клиент вызывает только методы из READ_ONLY_METHODS.
"""

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

PROD_URL = "https://invest-public-api.tbank.ru/rest"
SANDBOX_URL = "https://sandbox-invest-public-api.tbank.ru/rest"
PREFIX = "tinkoff.public.invest.api.contract.v1"

READ_ONLY_METHODS = {
    "UsersService/GetAccounts",
    "UsersService/GetInfo",
    "OperationsService/GetPortfolio",
    "OperationsService/GetPositions",
    "OperationsService/GetOperationsByCursor",
    "InstrumentsService/GetInstrumentBy",
    "InstrumentsService/ShareBy",
    "InstrumentsService/BondBy",
    "InstrumentsService/EtfBy",
    "InstrumentsService/GetBondCoupons",
    "MarketDataService/GetLastPrices",
}

# Сертификаты T-Bank подписаны корневым CA Минцифры, которого нет в стандартных
# хранилищах. Добавляем его к системному бандлу, а не отключаем проверку TLS.
RU_ROOT_CA = Path(__file__).with_name("russian_trusted_root_ca.pem")
RU_ROOT_CA_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


def _ca_bundle() -> str:
    import ssl

    pem = RU_ROOT_CA.read_text()
    der = ssl.PEM_cert_to_DER_cert(pem)
    if hashlib.sha256(der).hexdigest() != RU_ROOT_CA_SHA256:
        raise RuntimeError(f"Отпечаток {RU_ROOT_CA.name} не совпадает с ожидаемым")

    base = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if not base:
        import certifi

        base = certifi.where()
    bundle = Path(tempfile.gettempdir()) / "tinvest_ca_bundle.pem"
    bundle.write_text(Path(base).read_text() + "\n" + pem)
    return str(bundle)


class TInvestError(Exception):
    pass


class TInvestClient:
    def __init__(self, token: str | None = None, sandbox: bool = False):
        token = token or os.environ.get("TINVEST_TOKEN")
        if not token:
            raise TInvestError("Не найден токен: задайте переменную окружения TINVEST_TOKEN")
        self.base_url = SANDBOX_URL if sandbox else PROD_URL
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        # verify передаём в каждый запрос: session.verify requests перетирает
        # значением REQUESTS_CA_BUNDLE из окружения
        self.ca_bundle = _ca_bundle()

    def call(self, method: str, body: dict | None = None) -> dict:
        if method not in READ_ONLY_METHODS:
            raise TInvestError(f"Метод {method} не входит в список разрешённых (только чтение)")
        try:
            resp = self.session.post(
                f"{self.base_url}/{PREFIX}.{method}", json=body or {}, timeout=30, verify=self.ca_bundle
            )
        except requests.RequestException as e:
            raise TInvestError(f"{method}: сетевая ошибка: {e}") from e
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = f"{err.get('message')} (code {err.get('code')}, {err.get('description')})"
            except ValueError:
                msg = resp.text[:300]
            raise TInvestError(f"{method}: HTTP {resp.status_code}: {msg}")
        return resp.json()


def q(value: dict | None) -> Decimal:
    """Quotation / MoneyValue -> Decimal. units приходит строкой (int64)."""
    if not value:
        return Decimal(0)
    return Decimal(str(value.get("units", 0))) + Decimal(value.get("nano", 0)) / Decimal(10**9)


def ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # API может отдавать наносекунды, а fromisoformat понимает максимум микросекунды
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(value)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
