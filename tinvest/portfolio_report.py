"""Отчёт по портфелю в T-Invest: позиции, структура, облигации и купоны на год вперёд.

Запуск:
    export TINVEST_TOKEN=...   # токен только на чтение
    python tinvest/portfolio_report.py                 # отчёт в stdout
    python tinvest/portfolio_report.py --out report.md # отчёт в файл
    python tinvest/portfolio_report.py --raw raw/      # плюс сырые ответы API в JSON
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from client import TInvestClient, TInvestError, iso, q, ts  # noqa: E402

TYPE_NAMES = {
    "share": "Акции",
    "bond": "Облигации",
    "etf": "Фонды",
    "currency": "Валюта",
    "futures": "Фьючерсы",
    "option": "Опционы",
    "sp": "Структурные продукты",
}
INFO_METHODS = {
    "share": "InstrumentsService/ShareBy",
    "bond": "InstrumentsService/BondBy",
    "etf": "InstrumentsService/EtfBy",
}


def money(x: Decimal | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{digits}f}".replace(",", " ")


def pct(x: Decimal | None) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


class Report:
    def __init__(self, client: TInvestClient, raw_dir: Path | None):
        self.client = client
        self.raw_dir = raw_dir
        self._info_cache: dict[str, dict] = {}

    def call(self, method: str, body: dict | None = None, raw_name: str | None = None) -> dict:
        data = self.client.call(method, body)
        if self.raw_dir and raw_name:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / f"{raw_name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return data

    def instrument(self, uid: str, itype: str) -> dict:
        if uid not in self._info_cache:
            method = INFO_METHODS.get(itype, "InstrumentsService/GetInstrumentBy")
            body = {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}
            try:
                self._info_cache[uid] = self.call(method, body, f"instrument_{uid}")["instrument"]
            except TInvestError as e:
                print(f"Не удалось получить данные инструмента {uid}: {e}", file=sys.stderr)
                self._info_cache[uid] = {}
        return self._info_cache[uid]

    def coupons(self, uid: str, now: datetime) -> list[dict]:
        body = {"instrumentId": uid, "from": iso(now), "to": iso(now + timedelta(days=365))}
        try:
            return self.call("InstrumentsService/GetBondCoupons", body, f"coupons_{uid}").get("events", [])
        except TInvestError as e:
            print(f"Не удалось получить купоны {uid}: {e}", file=sys.stderr)
            return []

    def account_section(self, account: dict, now: datetime) -> str:
        acc_id = account["id"]
        pf = self.call(
            "OperationsService/GetPortfolio",
            {"accountId": acc_id, "currency": "RUB"},
            f"portfolio_{acc_id}",
        )
        total = q(pf.get("totalAmountPortfolio"))
        out = [f"## Счёт: {account.get('name') or acc_id}", ""]
        out.append(f"Стоимость портфеля: **{money(total)} ₽**")
        exp = pf.get("expectedYield")
        if exp:
            out.append(f"Текущая доходность портфеля: {q(exp):.2f}%")
        out.append("")

        positions = []
        for p in pf.get("positions", []):
            itype = p.get("instrumentType", "")
            uid = p.get("instrumentUid", "")
            info = self.instrument(uid, itype) if uid else {}
            qty = q(p.get("quantity"))
            price = q(p.get("currentPrice"))
            nkd = q(p.get("currentNkd"))
            avg = q(p.get("averagePositionPrice"))
            cur = (p.get("currentPrice") or {}).get("currency", "rub").lower()
            value = qty * (price + nkd)
            cost = qty * avg
            pnl = q(p.get("expectedYield"))
            positions.append(
                {
                    "uid": uid,
                    "type": itype,
                    "ticker": info.get("ticker") or p.get("figi", "?"),
                    "name": info.get("name", ""),
                    "sector": info.get("sector") or "не указан",
                    "info": info,
                    "qty": qty,
                    "price": price,
                    "nkd": nkd,
                    "avg": avg,
                    "cur": cur,
                    "value": value,
                    "pnl": pnl,
                    "pnl_pct": pnl / cost if cost else None,
                    # Доля считается только для рублёвых позиций: total в рублях
                    "share": value / total if total and cur == "rub" else None,
                }
            )
        positions.sort(key=lambda x: x["value"] if x["cur"] == "rub" else Decimal(0), reverse=True)

        # Структура по классам: берём итоги, которые считает сам брокер
        out.append("### Структура по классам активов")
        rows = []
        for key, field in [
            ("share", "totalAmountShares"),
            ("bond", "totalAmountBonds"),
            ("etf", "totalAmountEtf"),
            ("currency", "totalAmountCurrencies"),
            ("futures", "totalAmountFutures"),
            ("option", "totalAmountOptions"),
            ("sp", "totalAmountSp"),
        ]:
            amount = q(pf.get(field))
            if amount:
                rows.append([TYPE_NAMES[key], money(amount), pct(amount / total if total else None)])
        out += [table(["Класс", "Сумма, ₽", "Доля"], rows), ""]

        out.append("### Позиции")
        rows = [
            [
                p["ticker"],
                p["name"][:40],
                TYPE_NAMES.get(p["type"], p["type"]),
                money(p["qty"], 0) if p["qty"] == p["qty"].to_integral() else money(p["qty"]),
                f"{money(p['avg'])} {p['cur']}",
                f"{money(p['price'])} {p['cur']}",
                f"{money(p['value'])} {p['cur']}",
                money(p["pnl"]),
                pct(p["pnl_pct"]),
                pct(p["share"]),
            ]
            for p in positions
        ]
        out += [
            table(
                ["Тикер", "Название", "Тип", "Кол-во", "Ср. цена", "Цена", "Стоимость", "P/L", "P/L %", "Доля"],
                rows,
            ),
            "",
        ]
        if any(p["cur"] != "rub" for p in positions):
            out += ["Для позиций не в рублях доля не посчитана: цены в валюте инструмента.", ""]

        top = [p for p in positions if p["share"] is not None and p["type"] != "currency"][:5]
        if top:
            out.append("### Концентрация")
            out.append(f"Топ-5 позиций: {pct(sum((p['share'] for p in top), Decimal(0)))} портфеля.")
            big = [p for p in top if p["share"] > Decimal("0.10")]
            if big:
                out.append("Больше 10% портфеля: " + ", ".join(f"{p['ticker']} ({pct(p['share'])})" for p in big))
            out.append("")

        by_sector: dict[str, Decimal] = defaultdict(Decimal)
        for p in positions:
            if p["share"] is not None and p["type"] in ("share", "bond", "etf"):
                by_sector[p["sector"]] += p["value"]
        if by_sector:
            out.append("### По секторам (акции, облигации, фонды)")
            rows = [
                [s, money(v), pct(v / total)]
                for s, v in sorted(by_sector.items(), key=lambda kv: kv[1], reverse=True)
            ]
            out += [table(["Сектор", "Сумма, ₽", "Доля"], rows), ""]

        bonds = [p for p in positions if p["type"] == "bond"]
        if bonds:
            out += self.bonds_section(bonds, now)
        return "\n".join(out)

    def bonds_section(self, bonds: list[dict], now: datetime) -> list[str]:
        out = ["### Облигации"]
        rows = []
        income_by_month: dict[str, Decimal] = defaultdict(Decimal)
        total_income = Decimal(0)
        for b in bonds:
            info = b["info"]
            maturity = ts(info.get("maturityDate"))
            events = self.coupons(b["uid"], now)
            upcoming = sorted(
                (e for e in events if (ts(e.get("couponDate")) or now) >= now),
                key=lambda e: e.get("couponDate", ""),
            )
            income = Decimal(0)
            for e in upcoming:
                amount = q(e.get("payOneBond")) * b["qty"]
                income += amount
                income_by_month[ts(e["couponDate"]).strftime("%Y-%m")] += amount
            total_income += income
            nxt = upcoming[0] if upcoming else None
            flags = []
            if info.get("floatingCouponFlag"):
                flags.append("плавающий купон")
            if info.get("amortizationFlag"):
                flags.append("амортизация")
            if info.get("perpetualFlag"):
                flags.append("бессрочная")
            rows.append(
                [
                    b["ticker"],
                    b["name"][:35],
                    maturity.strftime("%d.%m.%Y") if maturity else "—",
                    money(q(info.get("nominal"))),
                    str(info.get("couponQuantityPerYear", "—")),
                    ts(nxt["couponDate"]).strftime("%d.%m.%Y") if nxt else "—",
                    money(q(nxt.get("payOneBond"))) if nxt else "—",
                    money(income),
                    ", ".join(flags) or "—",
                ]
            )
        out += [
            table(
                [
                    "Тикер",
                    "Название",
                    "Погашение",
                    "Номинал",
                    "Купонов/год",
                    "След. купон",
                    "Купон на 1 шт",
                    "Купоны за 12 мес, ₽",
                    "Особенности",
                ],
                rows,
            ),
            "",
            f"Купонный доход на 12 месяцев вперёд (до налога): **{money(total_income)} ₽**",
            "",
        ]
        if income_by_month:
            out.append("Купоны по месяцам:")
            out += [
                table(
                    ["Месяц", "Сумма, ₽"],
                    [[m, money(v)] for m, v in sorted(income_by_month.items())],
                ),
                "",
            ]
        out += [
            "Суммы для плавающих купонов, которые ещё не зафиксированы, API может отдавать нулями: "
            "реальный доход по ним будет выше.",
            "",
        ]
        return out

    def build(self, account_filter: str | None) -> str:
        now = datetime.now(timezone.utc)
        accounts = self.call("UsersService/GetAccounts", {}, "accounts").get("accounts", [])
        accounts = [a for a in accounts if a.get("status") == "ACCOUNT_STATUS_OPEN"]
        if account_filter:
            accounts = [a for a in accounts if a["id"] == account_filter]
        if not accounts:
            raise TInvestError("Не найдено ни одного открытого счёта (или токен не даёт к ним доступа)")
        parts = [f"# Портфель T-Invest на {now.astimezone().strftime('%d.%m.%Y %H:%M')}", ""]
        for acc in accounts:
            parts.append(self.account_section(acc, now))
        return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", help="ID счёта (по умолчанию все открытые)")
    parser.add_argument("--out", type=Path, help="Куда сохранить отчёт в Markdown")
    parser.add_argument("--raw", type=Path, help="Папка для сырых ответов API (для отладки)")
    parser.add_argument("--sandbox", action="store_true", help="Работать с песочницей")
    args = parser.parse_args()

    try:
        report = Report(TInvestClient(sandbox=args.sandbox), args.raw).build(args.account)
    except TInvestError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    if args.out:
        args.out.write_text(report)
        print(f"Отчёт сохранён в {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
