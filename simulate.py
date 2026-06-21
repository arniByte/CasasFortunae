"""
Casas Fortunae — headless battle simulator.

Гоняет реальный игровой движок (server.GameState) без браузера и без сети,
сводит партии бот-на-бот и собирает статистику для балансировки:
  • % побед по Домам (матрица Дом×Дом и общий),
  • % побед по стратегиям (матрица стратегий),
  • средняя длина партии в ходах,
  • как часто партии доходят до истощения колоды (fatigue / reshuffle),
  • как часто используются Долг Фортуне и реликвии,
  • преимущество первого хода.

Это инструмент Направления 1 из PLAN_FOR_OPUS.md — к нему возвращаемся при
каждой правке баланса, чтобы проверять изменения числами, а не «на глаз».

Запуск:
    python3 simulate.py                 # полный отчёт (по умолчанию)
    python3 simulate.py --games 800     # больше партий на матчап (точнее)
    python3 simulate.py --seed 7        # другой сид (детерминированный прогон)
    python3 simulate.py --out report.md # путь к markdown-отчёту

Боты — намеренно простые, прозрачные эвристики. Их задача — дать ФАКТИЧЕСКИЕ
числа про взаимодействие Домов и механик, а не быть сильным ИИ. «adaptive» —
кандидат на одиночного соперника (TODO в CLAUDE.md).
"""

import argparse
import random
import statistics
import time
from collections import defaultdict

# --- Детерминизм: подменяем источники энтропии в server на seeded random,
#     чтобы весь прогон воспроизводился по --seed. Делается ДО первого
#     GameState и только в симуляторе (server.py не трогаем). ---
import secrets as _secrets
_secrets.randbelow = lambda n: random.randrange(n) if n > 0 else 0
_secrets.token_hex = lambda n=32: "%0*x" % (n * 2, random.getrandbits(n * 8))

import server
from server import GameState, HOUSES, ELEMENTS

HOUSE_IDS = list(HOUSES.keys())            # medici, borgia, sforza, este
STRATEGIES = ["aggressor", "economist", "random", "adaptive"]

# ---------------------------------------------------------------------------
# Классификация карт по kind (для эвристик ботов)
# ---------------------------------------------------------------------------
DAMAGE_KINDS = {"damage", "damage_status", "damage_break", "damage_pierce",
                "charge", "surge", "draw_damage", "armor_damage"}
HEAL_KINDS   = {"heal", "heal_draw", "armor_heal", "florin_heal"}
ARMOR_KINDS  = {"armor", "armor_status", "armor_heal", "armor_blessing"}
ECON_KINDS   = {"gain_florin", "florin_draw", "florin_heal", "invest"}
DRAW_KINDS   = {"draw", "draw_discount", "status_draw", "codex", "peek_draw", "florin_draw"}


# Веса будущего урона от статусов (в эквиваленте престижа). Яд особенно ценен:
# тикает несколько ходов и игнорирует броню — поэтому вес выше прямого урона.
POISON_W, BLEED_W, BURN_W = 2.2, 1.3, 1.4


def est_damage(gs, p, opp, c):
    """Мгновенный урон по престижу прямо сейчас (для проверки на летальный ход)."""
    kind = c["kind"]
    if kind not in DAMAGE_KINDS:
        return 0
    val = gs._value(p, c)
    if kind == "charge":
        return val + (2 if p.armor > 0 else 0)
    if kind == "surge":
        return val + (2 if gs.last_blessed == gs.blessed else 0)
    if kind == "armor_damage":
        return p.armor
    if kind == "damage_pierce":
        return val + c["extra"].get("pierce", 0)
    return val


def dot_bonus(gs, p, opp, c):
    """Ценность накладываемого этой картой статуса-урона (яд/кровь/горение)."""
    ex = c["extra"]
    st, stk = ex.get("status"), ex.get("stacks", 0)
    bonus = 0.0
    if c["kind"] in ("damage_status", "status_draw", "status_self",
                     "poison_skip", "armor_status"):
        if st == "poison": bonus += stk * POISON_W
        elif st == "bleed": bonus += stk * BLEED_W
        elif st == "burn": bonus += stk * BURN_W
    if c["kind"] == "poison_double":
        base = gs._value(p, c)
        bonus += (base * 2 + opp.statuses.get("poison", 0)) * POISON_W if opp.has_status("poison") else base * POISON_W
    if c["kind"] == "poison_skip":
        bonus += gs._value(p, c) * POISON_W
    return bonus


def card_pressure(gs, seat, c, w):
    """Оценка пользы от розыгрыша карты СЕЙЧАС, в эквиваленте престижа.
    `w` — словарь весов по категориям (задаёт характер стратегии)."""
    p, opp = gs.players[seat], gs.players[1 - seat]
    kind, ex, val = c["kind"], c["extra"], gs._value(p, c)
    s = 0.0
    s += est_damage(gs, p, opp, c) * w["dmg"]
    s += dot_bonus(gs, p, opp, c) * w["dmg"]
    if kind in ("heal", "heal_draw"): s += val * w["heal"]
    if kind == "armor_heal": s += val * w["armor"] + ex.get("heal", 0) * w["heal"]
    if kind == "florin_heal": s += ex.get("heal", 0) * w["heal"] + val * w["econ"]
    if kind in ("armor", "armor_status", "armor_blessing"): s += val * w["armor"]
    if kind in ("gain_florin", "florin_draw", "invest", "counting"): s += max(val, 2) * w["econ"]
    if kind in ("draw", "draw_discount", "status_draw", "codex", "peek_draw", "florin_draw"):
        s += (ex.get("draw", val if kind == "draw" else 1) or 1) * w["draw"]
    if kind in ("status_self", "cleanse_self", "armor_blessing"): s += 0.8  # inspiration/blessing/cleanse
    if kind == "tax_enemy": s += 0.9
    if kind == "harmonic": s += 1.2
    if kind == "return_card": s += 0.7 if p.discard else -1
    if kind == "peek_respin": s += 0.4
    if kind == "thorns": s += (4 - 4 * 0.5)  # бьёт обоих; ситуативно
    return s


def affordable(gs, seat):
    p = gs.players[seat]
    return [c for c in p.hand if gs._cost(p, c) <= p.florins]


def _best_by_pressure(gs, seat, w, threshold=0.3):
    """Лучшая affordable-карта по pressure; None, если играть нечего полезного."""
    aff = affordable(gs, seat)
    if not aff:
        return None
    scored = [(card_pressure(gs, seat, c, w), c) for c in aff]
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] <= threshold:
        return None
    return scored[0][1]


def _lethal_card(gs, seat):
    """Карта, мгновенно закрывающая врага (престиж+броня), если такая есть в руке."""
    p, opp = gs.players[seat], gs.players[1 - seat]
    hp = opp.prestige + opp.armor
    best = None
    for c in affordable(gs, seat):
        d = est_damage(gs, p, opp, c)
        # break_armor/pierce игнорируют часть брони — учтём грубо
        if c["kind"] in ("damage_break",):
            d = gs._value(p, c)  # сравниваем с престижем, броню ломает
            if d >= opp.prestige:
                return c
        if d >= hp:
            best = c
    return best


# ---------------------------------------------------------------------------
# Политики ботов. choose() возвращает действие:
#   ("play", uid) | ("relic",) | ("debt",) | None  (None = закончить ход)
# ---------------------------------------------------------------------------
def _relic_ready(gs, seat):
    p = gs.players[seat]
    if p.house == "borgia":
        return (not p.relic_used_game) and gs.players[1 - seat].has_status("poison")
    if p.house == "sforza":
        return (not p.relic_used_turn) and p.armor >= 4
    if p.house == "medici":
        return (not p.relic_used_turn) and p.florins >= 3
    if p.house == "este":
        return not p.relic_used_turn
    return False


# Веса категорий по стратегиям (характер игры).
W_AGGRO = {"dmg": 1.30, "heal": 0.40, "armor": 0.35, "econ": 0.55, "draw": 0.75}
W_ECON  = {"dmg": 0.55, "heal": 0.80, "armor": 1.10, "econ": 1.35, "draw": 1.05}


def choose_aggressor(gs, seat):
    p = gs.players[seat]
    lc = _lethal_card(gs, seat)
    if lc:
        return ("play", lc["uid"])
    if p.house in ("sforza", "borgia") and _relic_ready(gs, seat):
        return ("relic",)
    best = _best_by_pressure(gs, seat, W_AGGRO, threshold=0.2)
    if best:
        return ("play", best["uid"])
    # Нет ничего по карману — взять Долг, если он откроет урон-карту
    if not p.debt:
        for c in p.hand:
            if c["kind"] in DAMAGE_KINDS and p.florins < gs._cost(p, c) <= p.florins + 3:
                return ("debt",)
    return None


def choose_economist(gs, seat):
    p = gs.players[seat]
    lc = _lethal_card(gs, seat)
    if lc:
        return ("play", lc["uid"])
    if p.house == "medici" and _relic_ready(gs, seat) and p.florins >= 6:
        return ("relic",)
    best = _best_by_pressure(gs, seat, W_ECON, threshold=0.4)
    if best:
        return ("play", best["uid"])
    if p.house == "sforza" and _relic_ready(gs, seat):
        return ("relic",)
    return None


def choose_random(gs, seat):
    p = gs.players[seat]
    options = [("play", c["uid"]) for c in affordable(gs, seat)]
    if _relic_ready(gs, seat):
        options.append(("relic",))
    if not p.debt and random.random() < 0.15:
        options.append(("debt",))
    if not options:
        return None
    # 18% шанс просто закончить ход — иначе бот спамил бы всю руку всегда
    if random.random() < 0.18:
        return None
    return random.choice(options)


def choose_adaptive(gs, seat):
    p, opp = gs.players[seat], gs.players[1 - seat]
    # 1) Летальный ход — добиваем
    lc = _lethal_card(gs, seat)
    if lc:
        return ("play", lc["uid"])
    # 2) Реликвия по ситуации
    if p.house in ("sforza", "borgia") and _relic_ready(gs, seat) and opp.prestige <= 14:
        return ("relic",)
    if p.house == "medici" and _relic_ready(gs, seat) and p.florins >= 6:
        return ("relic",)
    # 3) Динамические веса: лечимся под угрозой, давим когда враг низко, копим рано
    low = p.prestige <= 12
    closing = opp.prestige <= 12
    early = gs.turn <= 4 and p.prestige > 14
    w = {
        "dmg": 1.4 if closing else (0.85 if low else 1.0),
        "heal": 1.7 if low else 0.45,
        "armor": 1.2 if low else 0.65,
        "econ": 1.15 if early else 0.6,
        "draw": 1.0 if early else 0.8,
    }
    best = _best_by_pressure(gs, seat, w, threshold=0.3)
    if best:
        return ("play", best["uid"])
    if p.house == "este" and _relic_ready(gs, seat):
        return ("relic",)
    return None


CHOOSERS = {
    "aggressor": choose_aggressor,
    "economist": choose_economist,
    "random": choose_random,
    "adaptive": choose_adaptive,
}


# ---------------------------------------------------------------------------
# Один ход / одна партия
# ---------------------------------------------------------------------------
def take_turn(gs, seat, strategy, stats):
    chooser = CHOOSERS[strategy]
    guard = 0
    while gs.phase == "playing" and gs.active == seat:
        guard += 1
        if guard > 60:                       # страховка от зацикливания
            gs.end_turn(seat)
            return
        action = chooser(gs, seat)
        if action is None:
            gs.end_turn(seat)
            return
        kind = action[0]
        if kind == "play":
            err = gs.play_card(seat, action[1])
            if err is None:
                stats["cards_played"][seat] += 1
            else:
                gs.end_turn(seat)            # выбор недопустим — заканчиваем ход
                return
        elif kind == "relic":
            err = gs.use_relic(seat)
            if err is None:
                stats["relic_uses"][seat] += 1
            else:
                # реликвия недоступна — не зацикливаемся
                pass
        elif kind == "debt":
            err = gs.take_debt(seat)
            if err is None:
                stats["debt_uses"][seat] += 1


def play_game(house0, house1, strat0, strat1, max_turns=400):
    """Партия. seat0 всегда ходит первым (так устроен движок). Преимущество
    первого хода уравнивается на уровне матрицы — каждая пара Домов играется
    в обоих порядках. Возвращает dict с исходом и метриками."""
    def mk(name, house):
        return {"name": name, "house": house, "avatar": {"type": "svg", "id": 0}}

    gs = GameState(mk("P0", house0), mk("P1", house1))
    stats = {
        "cards_played": [0, 0],
        "relic_uses": [0, 0],
        "debt_uses": [0, 0],
    }
    while gs.phase == "playing" and gs.turn < max_turns:
        seat = gs.active
        strat = strat0 if seat == 0 else strat1
        take_turn(gs, seat, strat, stats)

    fatigue = any("истощён" in l for l in gs.log)
    reshuffle = any("перетасован" in l for l in gs.log)
    return {
        "winner": gs.winner,                 # 0 / 1 / None (если уперлись в max_turns)
        "turns": gs.turn,
        "houses": (house0, house1),
        "fatigue": fatigue,
        "reshuffle": reshuffle,
        "debt_used": stats["debt_uses"][0] + stats["debt_uses"][1] > 0,
        "relic_used": stats["relic_uses"][0] + stats["relic_uses"][1] > 0,
        # seat0 всегда первый → выигрыш seat0 = выигрыш первого хода
        "first_player_won": (gs.winner == 0) if gs.winner is not None else None,
    }


# ---------------------------------------------------------------------------
# Прогоны
# ---------------------------------------------------------------------------
def house_matrix(games_per_pair, strategy):
    """Все упорядоченные пары Дом×Дом одной стратегией (зеркало).
    Возвращает: wins[(hA,hB)] -> побед hA, плюс агрегаты."""
    wins = defaultdict(int)       # (rowHouse, colHouse) -> wins of rowHouse
    games = defaultdict(int)
    turns_all, fatigue_n, reshuffle_n, debt_n, relic_n = [], 0, 0, 0, 0
    fp_wins, fp_games = 0, 0
    draws = 0
    house_wins = defaultdict(int)
    house_games = defaultdict(int)

    for hA in HOUSE_IDS:
        for hB in HOUSE_IDS:
            for i in range(games_per_pair):
                r = play_game(hA, hB, strategy, strategy)
                games[(hA, hB)] += 1
                house_games[hA] += 1
                house_games[hB] += 1
                turns_all.append(r["turns"])
                fatigue_n += r["fatigue"]
                reshuffle_n += r["reshuffle"]
                debt_n += r["debt_used"]
                relic_n += r["relic_used"]
                if r["first_player_won"] is not None:
                    fp_games += 1
                    fp_wins += r["first_player_won"]
                if r["winner"] is None:
                    draws += 1
                    continue
                winner_house = hA if r["winner"] == 0 else hB
                loser_house = hB if r["winner"] == 0 else hA
                wins[(hA, hB)] += 1 if r["winner"] == 0 else 0
                house_wins[winner_house] += 1
    total = sum(games.values())
    return {
        "wins": wins, "games": games,
        "house_wins": house_wins, "house_games": house_games,
        "turns": turns_all, "fatigue": fatigue_n, "reshuffle": reshuffle_n,
        "debt": debt_n, "relic": relic_n, "draws": draws, "total": total,
        "fp_wins": fp_wins, "fp_games": fp_games,
    }


def strategy_matrix(games_per_pair):
    """Стратегия×стратегия с рандомными Домами — какая стратегия сильнее."""
    wins = defaultdict(int)       # (rowStrat, colStrat) -> wins of rowStrat
    games = defaultdict(int)
    for sA in STRATEGIES:
        for sB in STRATEGIES:
            for i in range(games_per_pair):
                hA = random.choice(HOUSE_IDS)
                hB = random.choice(HOUSE_IDS)
                r = play_game(hA, hB, sA, sB)
                games[(sA, sB)] += 1
                if r["winner"] == 0:
                    wins[(sA, sB)] += 1
    return {"wins": wins, "games": games}


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------
def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def fmt_matrix_md(title, row_ids, col_ids, cell_fn, name_fn=lambda x: x):
    lines = [f"### {title}", ""]
    header = "| ↓ vs → | " + " | ".join(name_fn(c) for c in col_ids) + " |"
    sep = "|" + "---|" * (len(col_ids) + 1)
    lines += [header, sep]
    for r in row_ids:
        row = [f"**{name_fn(r)}**"] + [cell_fn(r, c) for c in col_ids]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def build_report(args, hm, sm, elapsed):
    H = lambda hid: HOUSES[hid]["name"]
    out = []
    out.append("# Casas Fortunae — отчёт симулятора баланса\n")
    out.append(f"_Сгенерировано `simulate.py`. Сид: {args.seed}. "
               f"Партий на пару Домов: {args.games}. "
               f"Всего партий (матрица Домов): {hm['total']}. "
               f"Время: {elapsed:.1f} с._\n")
    out.append("> Боты — простые эвристики (см. шапку `simulate.py`). Матрица Домов "
               "сыграна стратегией **adaptive** в зеркале (обе стороны играют "
               "одинаково), чтобы изолировать силу самих Домов от стиля игры.\n")

    # --- Win-rate матрица Домов ---
    def cell(r, c):
        w = hm["wins"][(r, c)]
        g = hm["games"][(r, c)]
        if r == c:
            return f"{pct(w,g):.0f}%"      # зеркало — ~50% sanity check
        return f"{pct(w,g):.0f}%"
    out.append(fmt_matrix_md(
        "Win-rate матрица Домов (значение = % побед Дома-строки против Дома-столбца)",
        HOUSE_IDS, HOUSE_IDS, cell, H))
    out.append("> Диагональ (зеркало) должна быть ~50% — это проверка отсутствия "
               "перекоса первого хода в агрегате.\n")

    # --- Общий винрейт Домов ---
    out.append("### Общий винрейт по Домам (против всех, adaptive)\n")
    out.append("| Дом | Партий | Побед | Винрейт |")
    out.append("|---|---|---|---|")
    rows = []
    for hid in HOUSE_IDS:
        g = hm["house_games"][hid]
        w = hm["house_wins"][hid]
        rows.append((pct(w, g), hid, g, w))
    for wr, hid, g, w in sorted(rows, reverse=True):
        flag = " ⚠️" if (wr >= 56 or wr <= 44) else ""
        out.append(f"| {H(hid)} | {g} | {w} | **{wr:.1f}%**{flag} |")
    out.append("\n> ⚠️ = выход за коридор 44–56% (кандидат на правку баланса Домов, "
               "Направление 2 плана).\n")

    # --- Матрица стратегий ---
    def scell(r, c):
        w = sm["wins"][(r, c)]
        g = sm["games"][(r, c)]
        return f"{pct(w,g):.0f}%"
    out.append(fmt_matrix_md(
        "Win-rate матрица стратегий (рандомные Дома; % побед стратегии-строки)",
        STRATEGIES, STRATEGIES, scell))
    # сводный винрейт стратегий
    swins = defaultdict(int); sgames = defaultdict(int)
    for (a, b), g in sm["games"].items():
        w = sm["wins"][(a, b)]
        swins[a] += w; sgames[a] += g
        swins[b] += (g - w); sgames[b] += g
    out.append("**Сводный винрейт стратегий:** " + ", ".join(
        f"{s} {pct(swins[s], sgames[s]):.0f}%" for s in
        sorted(STRATEGIES, key=lambda s: -pct(swins[s], sgames[s]))) + "\n")

    # --- Темп и подсистемы ---
    turns = hm["turns"]
    out.append("### Темп партий и использование механик\n")
    out.append("| Метрика | Значение |")
    out.append("|---|---|")
    out.append(f"| Средняя длина партии | **{statistics.mean(turns):.1f}** ходов |")
    out.append(f"| Медиана длины | {statistics.median(turns):.0f} ходов |")
    out.append(f"| Мин / Макс | {min(turns)} / {max(turns)} ходов |")
    out.append(f"| Партий с истощением колоды (fatigue) | {pct(hm['fatigue'], hm['total']):.1f}% |")
    out.append(f"| Партий с перетасовкой сброса | {pct(hm['reshuffle'], hm['total']):.1f}% |")
    out.append(f"| Партий, где брали Долг Фортуне | {pct(hm['debt'], hm['total']):.1f}% |")
    out.append(f"| Партий с использованием реликвии | {pct(hm['relic'], hm['total']):.1f}% |")
    out.append(f"| Незавершённых (уперлись в лимит ходов) | {hm['draws']} |")
    out.append(f"| Винрейт первого хода | **{pct(hm['fp_wins'], hm['fp_games']):.1f}%** |")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Casas Fortunae balance simulator")
    ap.add_argument("--games", type=int, default=400,
                    help="партий на каждую пару Домов (по умолчанию 400)")
    ap.add_argument("--strat-games", type=int, default=200,
                    help="партий на каждую пару стратегий (по умолчанию 200)")
    ap.add_argument("--seed", type=int, default=1, help="сид RNG (детерминизм)")
    ap.add_argument("--out", default="SIM_REPORT.md", help="файл markdown-отчёта")
    ap.add_argument("--house-strategy", default="adaptive", choices=STRATEGIES,
                    help="стратегия для матрицы Домов (зеркало)")
    args = ap.parse_args()

    random.seed(args.seed)
    t0 = time.time()
    hm = house_matrix(args.games, args.house_strategy)
    sm = strategy_matrix(args.strat_games)
    elapsed = time.time() - t0

    report = build_report(args, hm, sm, elapsed)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    # короткая сводка в консоль
    print(report)
    print(f"\n[ok] Отчёт сохранён в {args.out}  ({elapsed:.1f} с, "
          f"{hm['total'] + sum(sm['games'].values())} партий всего)")


if __name__ == "__main__":
    main()
