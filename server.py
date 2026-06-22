"""
Casas Fortunae — двухпользовательская карточная игра в сеттинге Ренессанса (v2).
Сервер на Flask + flask-sock. Вся игровая логика на сервере (источник истины).

Новое в v2:
  - Дома (фракции): Медичи, Борджиа, Сфорца, Эсте — у каждого свой стиль и реликвия.
  - Система статусов: яд, кровотечение, горение, ослабление, вдохновение, благословение, укрепление.
  - Артефакты — легендарные карты, меняющие правила.
  - Аватары: портреты Ренессанса с Met Museum API (+ встроенный SVG-запас).

Запуск:  python server.py
"""

import json
import os
import random
import secrets
import string
import threading
import socket
import urllib.request
import ssl
from copy import deepcopy

from flask import Flask, render_template, request, jsonify, Response, send_from_directory

# Absolute paths based on this file's location — so the server works no matter
# which directory the user launches it from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}

from flask_sock import Sock
sock = Sock(app)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ELEMENTS = ["fire", "water", "earth", "air"]
ELEMENT_RU = {"fire": "Огонь", "water": "Вода", "earth": "Земля", "air": "Воздух"}
ELEMENT_LAT = {"fire": "Ignis", "water": "Aqua", "earth": "Terra", "air": "Aer"}
ELEMENT_GLYPH = {"fire": "🜂", "water": "🜄", "earth": "🜃", "air": "🜁"}

START_PRESTIGE = 30
START_HAND = 4
FLORIN_CAP = 10        # макс. прирост флоринов за один ход (от числа взятых ходов)
MAX_FLORINS = 15       # общий потолок накопленных флоринов — против "копи и взорви"
MAX_ARMOR = 20         # потолок брони — против бесконечной стены
CARDS_PER_TURN = 3     # лимит разыгранных карт за ход — выбор приоритета, а не спам
COMPASSION_THRESHOLD = 10  # престиж ≤ этого → «Сострадание Фортуны» (мягкий комебэк)

# ---------------------------------------------------------------------------
# Houses (factions)
# ---------------------------------------------------------------------------
HOUSES = {
    "medici": {
        "id": "medici", "name": "Медичи", "title": "Дом банкиров", "color": "#B8901F",
        "passive": "Покровительство: +2 флорина в начале каждого вашего хода.",
        "relic": {"name": "Золотая книга", "text": "Раз в ход: обменять 3 флорина на карту.", "id": "ledger"},
        "desc": "Богатство и темп. Заваливайте врага картами и переигрывайте в долгой партии.",
    },
    "borgia": {
        "id": "borgia", "name": "Борджиа", "title": "Дом интриганов", "color": "#6B2A40",
        "passive": "Яд в крови: первый урон за ход также накладывает 1 яд.",
        "relic": {"name": "Флакон Кантареллы", "text": "Раз за игру: удвоить весь яд на враге.", "id": "cantarella"},
        "desc": "Смерть от тысячи порезов. Яд игнорирует броню — стена врага бесполезна.",
    },
    "sforza": {
        "id": "sforza", "name": "Сфорца", "title": "Дом кондотьеров", "color": "#524A38",
        "passive": "Дисциплина: +1 броня в начале каждого хода.",
        "relic": {"name": "Знамя капитана", "text": "Раз в ход: обменять 4 брони на 4 урона.", "id": "banner"},
        "desc": "Несокрушимая стена, что карает за агрессию. Контратакуйте из-за щита.",
    },
    "este": {
        "id": "este", "name": "Эсте", "title": "Дом покровителей", "color": "#2E6A62",
        "passive": "Муза: благословлённые карты получают +2 вместо +1 и восстанавливают 1 престиж.",
        "relic": {"name": "Астролябия", "text": "Раз в ход: снять 1 яд (если есть) и получить +1 флорин; иначе — подсмотреть следующую стихию.", "id": "astrolabe"},
        "desc": "Оседлайте Колесо Фортуны. Взрывные ходы, когда стихия благословлена.",
    },
    "savonarola": {
        "id": "savonarola", "name": "Савонарола", "title": "Дом проповедников", "color": "#5A3320",
        "passive": "Праведный гнев: пока ваш престиж ниже вражеского, ваши карты урона бьют на +2.",
        "relic": {"name": "Власяница", "text": "Раз в ход: пожертвовать 2 престижа, нанести 4 урона в обход брони.", "id": "haircloth"},
        "desc": "Костёр тщеславия. Жертвуйте своим — престижем и картами — ради испепеляющих всплесков. Чем хуже ваши дела, тем праведнее гнев.",
    },
}

# ---------------------------------------------------------------------------
# Card catalogue
# ---------------------------------------------------------------------------
def C(cid, name, element, cost, text, kind, value=0, house=None, rarity="common", extra=None):
    return {"id": cid, "name": name, "element": element, "cost": cost, "text": text,
            "kind": kind, "value": value, "house": house, "rarity": rarity, "extra": extra or {}}

CATALOGUE = {
    # Fire
    "spark":      C("spark", "Искра", "fire", 1, "Нанести 2 урона.", "damage", 2),
    "torch":      C("torch", "Факельщик", "fire", 2, "Нанести 3 урона.", "damage", 3),
    "wildfire":   C("wildfire", "Дикий огонь", "fire", 4, "Нанести 3 урона и наложить Горение 2.", "damage_status", 3, extra={"status": "burn", "stacks": 2}),
    "pyre":       C("pyre", "Костёр", "fire", 5, "Нанести 5 урона, пробивая броню.", "damage_break", 5),
    "cannon":     C("cannon", "Залп пушек", "fire", 6, "Нанести 4 урона и 2 в обход брони.", "damage_pierce", 4, extra={"pierce": 2}),
    # Water
    "holy":       C("holy", "Святая вода", "water", 1, "Восстановить 2 престижа.", "heal", 2),
    "apoth":      C("apoth", "Аптекарь", "water", 2, "Восстановить 3 престижа.", "heal", 3),
    "tidal":      C("tidal", "Прилив", "water", 3, "Вернуть карту из сброса в руку.", "return_card", 0),
    "baptism":    C("baptism", "Крещение", "water", 5, "Восстановить 5 престижа и взять карту.", "heal_draw", 5, extra={"draw": 1}),
    "cleanse":    C("cleanse", "Очищающий дождь", "water", 4, "Снять все статусы с себя и получить Благословение.", "cleanse_self", 0),
    # Earth
    "ward":       C("ward", "Каменный оберег", "earth", 2, "Получить 3 брони.", "armor", 3),
    "rampart":    C("rampart", "Вал", "earth", 3, "Получить 6 брони.", "armor", 6),
    "gate":       C("gate", "Крепостные врата", "earth", 5, "Получить 8 брони и Укрепление.", "armor_status", 8, extra={"status": "fortified", "stacks": 1}),
    "patience":   C("patience", "Терпение горы", "earth", 4, "Получить 4 брони и 2 престижа.", "armor_heal", 4, extra={"heal": 2}),
    # Air
    "whisper":    C("whisper", "Шёпот", "air", 1, "Взять 1 карту.", "draw", 1),
    "courier":    C("courier", "Гонец", "air", 2, "Взять 2 карты.", "draw", 2),
    "wind":       C("wind", "Попутный ветер", "air", 2, "Получить Вдохновение (следующая карта бесплатна).", "status_self", 0, extra={"status": "inspiration", "stacks": 1}),
    "conspiracy": C("conspiracy", "Заговор", "air", 4, "Взять 2 карты; следующая карта на 1 дешевле.", "draw_discount", 2, extra={"discount": 1}),
    "sleight":    C("sleight", "Ловкость рук", "air", 3, "Взять 1 карту и нанести 2 урона.", "draw_damage", 2, extra={"draw": 1}),
    # Medici
    "loan_fl":    C("loan_fl", "Флорентийский заём", "neutral", 0, "Получить 3 флорина в этот ход.", "gain_florin", 3, house="medici"),
    "counting":   C("counting", "Счётный дом", "neutral", 2, "Получить 2 флорина и взять карту.", "florin_draw", 2, house="medici", extra={"draw": 1}),
    "bribe":      C("bribe", "Подкуп стражи", "neutral", 3, "Нанести 2 урона; следующая карта врага стоит на 2 дороже.", "tax_enemy", 2, house="medici", extra={"damage": 2}),
    "commission": C("commission", "Заказ мастеру", "neutral", 4, "Получить 4 флорина, 3 престижа и 1 карту.", "florin_heal", 4, house="medici", extra={"heal": 3, "draw": 1}),
    "banco":      C("banco", "Вклад банка", "neutral", 2, "Вложить 2 флорина — вернуть 6 в начале следующего хода.", "invest", 0, house="medici", extra={"invest": 2, "return": 6}),
    "condotta":   C("condotta", "Военный подряд", "neutral", 1, "Нанять кондотьеров: потратить до 8 флоринов, нанести столько же урона.", "spend_damage", 8, house="medici"),
    # Borgia
    "chalice":    C("chalice", "Отравленный кубок", "neutral", 2, "Нанести 2 урона и наложить Яд 2.", "damage_status", 2, house="borgia", extra={"status": "poison", "stacks": 2}),
    "lie":        C("lie", "Лживый шёпот", "neutral", 1, "Наложить Ослабление 1 и взять карту.", "status_draw", 0, house="borgia", extra={"status": "weaken", "stacks": 1, "draw": 1}),
    "apple":      C("apple", "Яблоко Борджиа", "neutral", 3, "Наложить Яд 3; удвоить, если враг уже отравлен.", "poison_double", 3, house="borgia", rarity="artifact"),
    "stiletto":   C("stiletto", "Стилет во тьме", "neutral", 3, "Нанести 3 урона и наложить Кровотечение.", "damage_status", 3, house="borgia", extra={"status": "bleed", "stacks": 1}),
    "silence":    C("silence", "Заговор молчания", "neutral", 5, "Наложить Яд 2; враг пропускает добор.", "poison_skip", 2, house="borgia"),
    # Sforza
    "pike":       C("pike", "Стена пик", "neutral", 4, "Получить 4 брони и Укрепление.", "armor_status", 4, house="sforza", extra={"status": "fortified", "stacks": 1}),
    "charge":     C("charge", "Натиск кондотьера", "neutral", 3, "Нанести 4 урона; +2, если у вас есть броня.", "charge", 4, house="sforza"),
    "riposte":    C("riposte", "Стойка рипоста", "neutral", 2, "Получить Благословение и 2 брони.", "armor_blessing", 2, house="sforza"),
    "siege":      C("siege", "Осадная машина", "neutral", 5, "Нанести 6 урона, пробивая броню.", "damage_break", 6, house="sforza"),
    "cannon_r":   C("cannon_r", "Королевская пушка", "neutral", 6, "Нанести урон, равный вашей броне (броня остаётся).", "armor_damage", 0, house="sforza", rarity="artifact"),
    # Este
    "astrologer": C("astrologer", "Чтение астролога", "neutral", 2, "Подсмотреть Колесо и взять карту.", "peek_draw", 0, house="este", extra={"draw": 1}),
    "surge":      C("surge", "Стихийный всплеск", "neutral", 2, "Нанести 4 урона (2 в обход брони); стрик — 6 (+2 пробоя).", "surge", 4, house="este"),
    "genius":     C("genius", "Покровитель гения", "neutral", 4, "Получить Вдохновение и взять 2 карты.", "status_draw", 0, house="este", extra={"status": "inspiration", "stacks": 1, "draw": 2}),
    "codex":      C("codex", "Кодекс Леонардо", "neutral", 5, "Взять 3 карты; следующие 3 карты на 1 дешевле.", "codex", 0, house="este", rarity="artifact"),
    "harmonic":   C("harmonic", "Гармония сфер", "neutral", 4, "Удвоить бонус благословения в этот ход.", "harmonic", 0, house="este"),
    # Savonarola — жертва/самопожертвование
    "immolate":   C("immolate", "Самосожжение", "fire", 3, "Пожертвовать 2 престижа; нанести 6 урона в обход брони.", "immolate", 6, house="savonarola", extra={"self": 2}),
    "tithe":      C("tithe", "Десятина", "neutral", 1, "Пожертвовать 2 престижа; получить 4 флорина и взять карту.", "tithe", 4, house="savonarola", extra={"self": 2, "draw": 1}),
    "penance":    C("penance", "Покаяние", "neutral", 2, "Сбросить карту; восстановить 4 престижа и получить Благословение.", "penance", 4, house="savonarola", extra={"discard": 1}),
    "zeal":       C("zeal", "Огонь веры", "fire", 2, "Нанести 3 урона +1 за каждые 4 недостающего престижа.", "zeal", 3, house="savonarola", extra={"per": 4}),
    "flagellant": C("flagellant", "Флагеллант", "neutral", 2, "Пожертвовать 2 престижа; наложить врагу Кровотечение 2 и взять карту.", "flagellant", 0, house="savonarola", extra={"self": 2, "stacks": 2, "draw": 1}),
    "martyr":     C("martyr", "Мученичество", "neutral", 4, "Нанести урон, равный половине вашего престижа (в обход брони); потерять столько же.", "martyr", 0, house="savonarola", rarity="artifact"),
    # Neutral
    "jester":     C("jester", "Придворный шут", "neutral", 2, "Подсмотреть Колесо; можно перекрутить.", "peek_respin", 0),
    "merchant":   C("merchant", "Заём купца", "neutral", 0, "Получить 2 флорина в этот ход.", "gain_florin", 2),
    "stone":      C("stone", "Философский камень", "neutral", 4, "Получить 5 флоринов и 5 престижа.", "florin_heal", 5, rarity="artifact", extra={"heal": 5}),
    "thorns":     C("thorns", "Терновый венец", "neutral", 4, "Оба игрока теряют 4 престижа; вы получаете 6 брони.", "thorns", 4, rarity="artifact"),
}

def build_house_deck(house_id):
    shared = ["spark", "spark", "torch", "wildfire", "pyre",
              "holy", "holy", "apoth", "baptism", "cleanse",
              "ward", "ward", "rampart", "patience",
              "whisper", "whisper", "courier", "wind", "sleight"]
    house_cards = {
        "medici":  ["loan_fl", "counting", "bribe", "commission", "banco", "condotta", "stone"],
        "borgia":  ["chalice", "chalice", "lie", "apple", "stiletto", "silence", "jester"],
        "sforza":  ["pike", "charge", "charge", "riposte", "siege", "cannon_r", "jester"],
        "este":    ["astrologer", "surge", "surge", "genius", "codex", "harmonic", "jester"],
        "savonarola": ["immolate", "tithe", "penance", "zeal", "zeal", "flagellant", "martyr"],
    }
    ids = shared + house_cards.get(house_id, [])
    deck = [deepcopy(CATALOGUE[cid]) for cid in ids if cid in CATALOGUE]
    for i, c in enumerate(deck):
        c["uid"] = f"{c['id']}_{i}_{secrets.token_hex(2)}"
    return deck


# ---------------------------------------------------------------------------
# Player & Game state
# ---------------------------------------------------------------------------
STATUS_GLYPH = {
    "poison": "☠", "bleed": "🩸", "burn": "🔥", "weaken": "🥀",
    "inspiration": "⚡", "blessing": "✨", "fortified": "🛡",
}
STATUS_RU = {
    "poison": "Яд", "bleed": "Кровотечение", "burn": "Горение", "weaken": "Ослабление",
    "inspiration": "Вдохновение", "blessing": "Благословение", "fortified": "Укрепление",
}

class PlayerState:
    def __init__(self, seat, name, house_id, avatar):
        self.seat = seat
        self.name = name
        self.house = house_id
        self.avatar = avatar          # dict: {"type":"met"/"svg", "url"/"id":...}
        self.prestige = START_PRESTIGE
        self.armor = 0
        self.florins = 0
        self.deck = build_house_deck(house_id)
        random.shuffle(self.deck)
        self.hand = []
        self.discard = []
        self.statuses = {}            # status -> stacks
        self.debt = False
        self.turns_taken = 0
        self.skip_next_draw = False
        self.next_discount = 0
        self.discount_charges = 0     # codex: N cards at -1
        self.tax = 0
        self.invest_return = 0        # banco payout next turn
        self.relic_used_turn = False  # once-per-turn relic
        self.relic_used_game = False  # once-per-game relic
        self.first_damage_done = False  # borgia venom tracking
        self.harmonic = False         # este: blessed bonus doubled this turn
        self.reshuffled = False
        self.cards_played = 0         # сыграно карт в текущий ход (лимит CARDS_PER_TURN)

    def add_status(self, status, stacks):
        self.statuses[status] = self.statuses.get(status, 0) + stacks
        if self.statuses[status] <= 0:
            self.statuses.pop(status, None)

    def has_status(self, status):
        return self.statuses.get(status, 0) > 0

    def draw(self, n=1, log=None):
        for _ in range(n):
            if not self.deck:
                if self.discard and not self.reshuffled:
                    self.deck = self.discard; self.discard = []
                    random.shuffle(self.deck); self.reshuffled = True
                    if log is not None: log.append(f"{self.name}: колода перетасована.")
                else:
                    self.prestige -= 2
                    if log is not None: log.append(f"{self.name} истощён: −2 престижа.")
                    continue
            self.hand.append(self.deck.pop())


class GameState:
    def __init__(self, p0, p1):
        # p0, p1 are dicts: {name, house, avatar}
        self.players = [
            PlayerState(0, p0["name"], p0["house"], p0["avatar"]),
            PlayerState(1, p1["name"], p1["house"], p1["avatar"]),
        ]
        self.active = 0
        self.turn = 0
        self.blessed = "fire"
        self.last_blessed = None
        self.wheel_angle = 0.0
        self.phase = "playing"
        self.log = []
        self.winner = None
        self.can_respin = False
        self.compassion = False       # «Сострадание Фортуны» сработало в этот ход
        self.peeked_next = None       # for astrolabe/jester peek
        self.rng = random.Random(secrets.randbelow(2**31))
        for p in self.players:
            p.draw(START_HAND)
        self.start_turn(first=True)

    # ---- wheel ----
    def spin_wheel(self, force=None):
        self.last_blessed = self.blessed
        elem = force if force in ELEMENTS else self.rng.choice(ELEMENTS)
        idx = ELEMENTS.index(elem)
        quadrant = 90 * idx
        self.wheel_angle = 360 * 4 + quadrant + self.rng.uniform(-28, 28)
        self.blessed = elem
        return elem

    def _compassion_element(self, p):
        """Стихия, которой у игрока больше всего карт в руке (только благословляемые,
        не neutral). Ничья — случайно среди лидеров (детерминировано по rng).
        None, если благословляемых карт в руке нет."""
        counts = {}
        for c in p.hand:
            e = c["element"]
            if e in ELEMENTS:
                counts[e] = counts.get(e, 0) + 1
        if not counts:
            return None
        best = max(counts.values())
        cands = [e for e in ELEMENTS if counts.get(e, 0) == best]
        return self.rng.choice(cands)

    def start_turn(self, first=False):
        p = self.players[self.active]
        opp = self.players[1 - self.active]
        self.turn += 1
        p.turns_taken += 1
        p.relic_used_turn = False
        p.first_damage_done = False
        p.harmonic = False
        p.cards_played = 0

        # Wheel
        self.compassion = False
        if opp.debt:
            # Соперник управляет твоим вращением — Долг сильнее Сострадания.
            self.spin_wheel()
            opp.debt = False
            self.log.append(f"Фортуна в руках соперника! {ELEMENT_RU[self.blessed]}.")
        else:
            force = None
            if not first and p.prestige <= COMPASSION_THRESHOLD:
                force = self._compassion_element(p)
            self.spin_wheel(force=force)
            if force:
                self.compassion = True
                self.log.append(f"Сострадание Фортуны: Колесо благоволит {ELEMENT_RU[self.blessed]} — Дом в беде.")
            else:
                self.log.append(f"Колесо: {ELEMENT_RU[self.blessed]} благословлён.")

        # Fortune streak bonus
        if self.last_blessed == self.blessed and not first:
            p.florins += 1
            self.log.append(f"Благосклонность Фортуны: +1 флорин ({ELEMENT_RU[self.blessed]} дважды).")

        # start-of-turn statuses
        self._tick_statuses(p)

        # Florins
        p.florins += min(p.turns_taken, FLORIN_CAP)
        # House Medici passive
        if p.house == "medici":
            p.florins += 2
        # invest payout
        if p.invest_return:
            p.florins += p.invest_return
            self.log.append(f"{p.name}: вклад вернул {p.invest_return} флоринов.")
            p.invest_return = 0
        # House Sforza passive
        if p.house == "sforza":
            p.armor += 1

        # reset per-turn
        p.next_discount = 0
        p.tax = 0

        # draw
        if not (first and self.active == 0):
            if p.skip_next_draw:
                p.skip_next_draw = False
                self.log.append(f"{p.name} пропускает добор.")
            else:
                p.draw(1, self.log)

        self._clamp_resources()
        self._check_over()

    def _tick_statuses(self, p):
        # poison: lose = stacks, ignores armor, then -1
        if p.has_status("poison"):
            dmg = p.statuses["poison"]
            p.prestige -= dmg
            p.add_status("poison", -1)
            self.log.append(f"{p.name}: яд наносит {dmg} (осталось {p.statuses.get('poison',0)}).")
        # burn: lose = stacks, then halve
        if p.has_status("burn"):
            dmg = p.statuses["burn"]
            p.prestige -= dmg
            new = dmg // 2
            p.statuses["burn"] = new
            if new <= 0: p.statuses.pop("burn", None)
            self.log.append(f"{p.name}: горение наносит {dmg}.")
        # weaken/bleed decay handled per-turn
        for s in ("weaken", "bleed"):
            if p.has_status(s):
                p.add_status(s, -1)

    # ---- damage/heal core ----
    def _deal(self, target, amount, pierce=0, break_armor=False, source=None):
        """Deal damage. Blessing absorbs one instance. Armor first unless pierce/break."""
        # weaken on source reduces damage
        if source is not None and source.has_status("weaken"):
            amount = max(0, amount - 1)
        # Savonarola «Праведный гнев»: +2 урона врагу, пока вы отстаёте по престижу
        if source is not None and source is not target and source.house == "savonarola" \
                and source.prestige < target.prestige and (amount + pierce) > 0:
            if amount > 0: amount += 2
            else: pierce += 2
        # blessing absorbs the whole instance
        if target.has_status("blessing") and (amount + pierce) > 0:
            target.add_status("blessing", -1)
            self.log.append(f"{target.name}: благословение поглотило урон.")
            return
        if break_armor and not target.has_status("fortified"):
            target.armor = 0
        through = amount
        if not break_armor:
            absorbed = min(target.armor, through)
            target.armor -= absorbed
            through -= absorbed
        target.prestige -= (through + pierce)
        # Borgia venom: first damage each turn applies poison
        if source is not None and source.house == "borgia" and not source.first_damage_done and (through + pierce) > 0:
            source.first_damage_done = True
            target.add_status("poison", 1)
            self.log.append(f"{target.name}: яд Борджиа (+1).")

    def _apply_status(self, target, status, stacks):
        # blessing absorbs one negative status application
        negative = status in ("poison", "bleed", "burn", "weaken")
        if negative and target.has_status("blessing"):
            target.add_status("blessing", -1)
            self.log.append(f"{target.name}: благословение сняло {STATUS_RU[status]}.")
            return
        target.add_status(status, stacks)

    def _clamp_resources(self):
        """Потолки флоринов и брони обоим игрокам — против бесконечного
        накопления ('копи и взорви за один ход') и нерушимой стены."""
        for p in self.players:
            if p.florins > MAX_FLORINS:
                p.florins = MAX_FLORINS
            if p.armor > MAX_ARMOR:
                p.armor = MAX_ARMOR

    # ---- cost ----
    def _cost(self, p, c):
        cost = c["cost"]
        if c["element"] == self.blessed and c["element"] != "neutral":
            cost = max(1, cost - 1)
        if p.has_status("inspiration"):
            cost = 0
        if p.next_discount:
            cost = max(0, cost - p.next_discount)
        if p.discount_charges > 0:
            cost = max(0, cost - 1)
        if p.tax:
            cost += p.tax
        return max(0, cost)

    def _value(self, p, c):
        bonus = 0
        if c["element"] == self.blessed and c["element"] != "neutral":
            bonus = 2 if p.house == "este" else 1
            if p.harmonic:
                bonus *= 2
        return c["value"] + bonus

    # ---- threat estimation (for "под угрозой" indicator) ----
    def _est_damage(self, src, tgt, c):
        """Сколько престижа снимет карта c (src→tgt), если сыграть её сейчас.
        Чистая оценка без мутаций — зеркалит логику _deal. Благословение врага
        поглощает удар целиком, поэтому считаем 0."""
        kind, ex = c["kind"], c["extra"]
        val = self._value(src, c)
        if src.has_status("weaken"):
            val = max(0, val - 1)
        armor = tgt.armor
        blessed = tgt.has_status("blessing")
        fortified = tgt.has_status("fortified")

        def through(amount, pierce=0):
            if blessed:
                return 0
            absorbed = min(armor, amount)
            return (amount - absorbed) + pierce

        if kind in ("damage", "damage_status", "draw_damage"):
            return through(val)
        if kind == "damage_break":
            if blessed:
                return 0
            return max(0, val - armor) if fortified else val
        if kind == "damage_pierce":
            return through(val, pierce=ex.get("pierce", 0))
        if kind == "charge":
            return through(val + (2 if src.armor > 0 else 0))
        if kind == "surge":
            streak = (self.last_blessed == self.blessed)
            return through(val + 2 if streak else val, pierce=2)
        if kind == "armor_damage":
            return through(min(src.armor, 10))
        if kind == "tax_enemy":
            return through(ex.get("damage", 0))
        if kind == "thorns":
            return through(val)
        return 0

    def _self_cost(self, p, c):
        """Сколько престижа карта снимает с самого игрока (жертвы Савонаролы)."""
        kind, ex = c["kind"], c["extra"]
        if kind in ("immolate", "tithe", "flagellant"):
            return ex.get("self", 0)
        if kind == "martyr":
            return (p.prestige + 1) // 2
        return 0

    def _lethal_threat(self, atk_seat, def_seat):
        """True, если защитник рискует погибнуть к началу своего следующего хода:
        либо собственный DoT (яд+горение) уже смертелен, либо у атакующего есть
        карта по карману (с учётом дохода будущего хода), закрывающая его одним
        ударом. Только булев сигнал — карты соперника не раскрываются."""
        atk, dfn = self.players[atk_seat], self.players[def_seat]
        if dfn.prestige <= 0 or self.phase != "playing":
            return False
        # DoT, который тикнет в начале хода защитника (яд и горение бьют сразу)
        dot = dfn.statuses.get("poison", 0) + dfn.statuses.get("burn", 0)
        if dot >= dfn.prestige:
            return True
        # Бюджет флоринов атакующего на его следующем ходу (доход + пассивки)
        income = min(atk.turns_taken + 1, FLORIN_CAP)
        if atk.house == "medici":
            income += 2
        budget = min(MAX_FLORINS, atk.florins + income + atk.invest_return)
        for c in atk.hand:
            if self._cost(atk, c) <= budget and self._est_damage(atk, dfn, c) >= dfn.prestige:
                return True
        return False

    # ---- play card ----
    def play_card(self, seat, uid):
        if self.phase != "playing": return "Игра окончена."
        if seat != self.active: return "Сейчас не ваш ход."
        p = self.players[seat]
        opp = self.players[1 - seat]
        c = next((x for x in p.hand if x.get("uid") == uid), None)
        if not c: return "Карты нет в руке."
        if p.cards_played >= CARDS_PER_TURN:
            return f"Предел хода: не более {CARDS_PER_TURN} карт."
        # Жертвенные карты Савонаролы не должны приводить к самопоражению по ошибке
        sc = self._self_cost(p, c)
        if sc > 0 and sc >= p.prestige:
            return "Эта жертва погубит вас — слишком мало престижа."
        cost = self._cost(p, c)
        if p.florins < cost: return "Недостаточно флоринов."

        p.florins -= cost
        # consume cost modifiers
        had_inspiration = p.has_status("inspiration")
        if had_inspiration:
            p.add_status("inspiration", -1)
        if p.next_discount:
            p.next_discount = 0
        if p.discount_charges > 0:
            p.discount_charges -= 1
        if p.tax:
            p.tax = 0

        # bleed: playing a card costs prestige
        if p.has_status("bleed"):
            p.prestige -= 1
            self.log.append(f"{p.name}: кровотечение −1.")

        val = self._value(p, c)
        kind = c["kind"]
        ex = c["extra"]
        nm = c["name"]

        if kind == "damage":
            self._deal(opp, val, source=p); self.log.append(f"{p.name} → «{nm}»: {val} урона.")
        elif kind == "damage_status":
            self._deal(opp, val, source=p)
            self._apply_status(opp, ex["status"], ex["stacks"])
            self.log.append(f"{p.name} → «{nm}»: {val} урона + {STATUS_RU[ex['status']]} {ex['stacks']}.")
        elif kind == "damage_break":
            self._deal(opp, val, break_armor=True, source=p); self.log.append(f"{p.name} → «{nm}»: {val}, броня пробита.")
        elif kind == "damage_pierce":
            self._deal(opp, val, pierce=ex["pierce"], source=p); self.log.append(f"{p.name} → «{nm}»: {val}+{ex['pierce']} (пробой).")
        elif kind == "heal":
            p.prestige = min(START_PRESTIGE, p.prestige + val); self.log.append(f"{p.name} лечится на {val}.")
        elif kind == "heal_draw":
            p.prestige = min(START_PRESTIGE, p.prestige + val); p.draw(ex.get("draw",1), self.log); self.log.append(f"{p.name}: +{val} престижа, +карта.")
        elif kind == "armor":
            p.armor += val; self.log.append(f"{p.name}: +{val} брони.")
        elif kind == "armor_status":
            p.armor += val; self._apply_status(p, ex["status"], ex["stacks"]); self.log.append(f"{p.name}: +{val} брони + {STATUS_RU[ex['status']]}.")
        elif kind == "armor_heal":
            p.armor += val; p.prestige = min(START_PRESTIGE, p.prestige + ex.get("heal",0)); self.log.append(f"{p.name}: +{val} брони, +{ex.get('heal',0)} престижа.")
        elif kind == "armor_blessing":
            p.armor += val; self._apply_status(p, "blessing", 1); self.log.append(f"{p.name}: +{val} брони + Благословение.")
        elif kind == "armor_damage":
            dmg = min(p.armor, 10); self._deal(opp, dmg, source=p); self.log.append(f"{p.name}: пушка бьёт на {dmg}.")
        elif kind == "charge":
            d = val + (2 if p.armor > 0 else 0); self._deal(opp, d, source=p); self.log.append(f"{p.name} → «{nm}»: {d} урона.")
        elif kind == "draw":
            p.draw(val, self.log); self.log.append(f"{p.name}: +{val} карт.")
        elif kind == "draw_discount":
            p.draw(val, self.log); p.next_discount = max(p.next_discount, ex.get("discount",1)); self.log.append(f"{p.name}: +{val} карт, скидка.")
        elif kind == "draw_damage":
            p.draw(ex.get("draw",1), self.log); self._deal(opp, val, source=p); self.log.append(f"{p.name}: +карта, {val} урона.")
        elif kind == "status_self":
            self._apply_status(p, ex["status"], ex["stacks"]); self.log.append(f"{p.name}: {STATUS_RU[ex['status']]}.")
        elif kind == "status_draw":
            self._apply_status(opp if ex["status"] in ("weaken","poison","bleed") else p, ex["status"], ex["stacks"])
            p.draw(ex.get("draw",1), self.log)
            tgt = "врагу" if ex["status"] in ("weaken","poison","bleed") else "себе"
            self.log.append(f"{p.name}: {STATUS_RU[ex['status']]} {tgt}, +{ex.get('draw',1)} карт.")
        elif kind == "cleanse_self":
            p.statuses.clear(); self._apply_status(p, "blessing", 1); self.log.append(f"{p.name}: статусы сняты, Благословение.")
        elif kind == "gain_florin":
            p.florins += val; self.log.append(f"{p.name}: +{val} флоринов.")
        elif kind == "florin_draw":
            p.florins += val; p.draw(ex.get("draw",1), self.log); self.log.append(f"{p.name}: +{val} флоринов, +карта.")
        elif kind == "florin_heal":
            p.florins += val; p.prestige = min(START_PRESTIGE, p.prestige + ex.get("heal",0))
            if ex.get("draw"): p.draw(ex["draw"], self.log)
            self.log.append(f"{p.name}: +{val} флоринов, +{ex.get('heal',0)} престижа{', +карта' if ex.get('draw') else ''}.")
        elif kind == "tax_enemy":
            opp.tax += val
            if ex.get("damage"):
                self._deal(opp, ex["damage"], source=p)
            self.log.append(f"{p.name}: карта врага дороже на {val}" + (f", {ex['damage']} урона" if ex.get('damage') else "") + ".")
        elif kind == "spend_damage":
            d = min(p.florins, val); p.florins -= d; self._deal(opp, d, source=p)
            self.log.append(f"{p.name} → «{nm}»: потрачено {d} флоринов, {d} урона.")
        elif kind == "invest":
            if p.florins >= ex["invest"]:
                p.florins -= ex["invest"]; p.invest_return += ex["return"]; self.log.append(f"{p.name}: вложил {ex['invest']}, вернётся {ex['return']}.")
            else:
                p.florins += cost; self.log.append(f"{p.name}: недостаточно для вклада.")  # refund
        elif kind == "poison_double":
            base = val
            if opp.has_status("poison"):
                current = opp.statuses.get("poison", 0)
                opp.statuses["poison"] = (current + base) * 2
                self.log.append(f"{p.name} → «{nm}»: яд удвоен до {opp.statuses['poison']}.")
            else:
                self._apply_status(opp, "poison", base); self.log.append(f"{p.name} → «{nm}»: Яд {base}.")
        elif kind == "poison_skip":
            self._apply_status(opp, "poison", val); opp.skip_next_draw = True; self.log.append(f"{p.name}: Яд {val}, враг пропустит добор.")
        elif kind == "surge":
            streak = (self.last_blessed == self.blessed)
            dmg = val + 2 if streak else val
            self._deal(opp, dmg, pierce=2, source=p)
            self.log.append(f"{p.name} → «{nm}»: {dmg}+2пробой" + (" (стрик!)." if streak else "."))
        elif kind == "codex":
            p.draw(3, self.log); p.discount_charges = 3; self.log.append(f"{p.name} → «{nm}»: +3 карты, скидка на 3 карты.")
        elif kind == "harmonic":
            p.harmonic = True; self.log.append(f"{p.name} → «{nm}»: бонус благословения удвоен.")
        elif kind == "thorns":
            self._deal(p, 4); self._deal(opp, 4, source=p); p.armor += 6; self.log.append(f"{p.name} → «{nm}»: оба −4, вы +6 брони.")
        elif kind == "return_card":
            if p.discard:
                r = p.discard.pop(); p.hand.append(r); self.log.append(f"{p.name} вернул «{r['name']}».")
            else:
                self.log.append(f"{p.name}: сброс пуст.")
        elif kind == "peek_draw":
            self.peeked_next = self._preview_spin(); p.draw(ex.get("draw",1), self.log); self.log.append(f"{p.name}: подсмотрел Колесо, +карта.")
        elif kind == "peek_respin":
            self.can_respin = True; self.peeked_next = self._preview_spin(); self.log.append(f"{p.name}: шут показал Колесо.")
        elif kind == "immolate":
            p.prestige -= ex.get("self", 0)
            self._deal(opp, 0, pierce=val, source=p)
            self.log.append(f"{p.name} → «{nm}»: −{ex.get('self',0)} себе, {val} в обход брони.")
        elif kind == "tithe":
            p.prestige -= ex.get("self", 0); p.florins += val; p.draw(ex.get("draw",1), self.log)
            self.log.append(f"{p.name} → «{nm}»: −{ex.get('self',0)} престижа, +{val} флоринов, +карта.")
        elif kind == "penance":
            n = ex.get("discard", 1); dropped = 0
            for _ in range(n):
                # сбрасываем самую дорогую другую карту руки (кроме самой penance)
                others = [x for x in p.hand if x.get("uid") != uid]
                if not others: break
                victim = max(others, key=lambda x: x["cost"])
                p.hand.remove(victim); p.discard.append(victim); dropped += 1
            p.prestige = min(START_PRESTIGE, p.prestige + val); self._apply_status(p, "blessing", 1)
            self.log.append(f"{p.name} → «{nm}»: сброшено {dropped}, +{val} престижа, Благословение.")
        elif kind == "zeal":
            per = ex.get("per", 5); missing = max(0, START_PRESTIGE - p.prestige)
            d = val + missing // per
            self._deal(opp, d, source=p)
            self.log.append(f"{p.name} → «{nm}»: {d} урона (гнев растёт в беде).")
        elif kind == "flagellant":
            p.prestige -= ex.get("self", 0); self._apply_status(opp, "bleed", ex.get("stacks", 2)); p.draw(ex.get("draw",1), self.log)
            self.log.append(f"{p.name} → «{nm}»: −{ex.get('self',0)} себе, Кровотечение {ex.get('stacks',2)} врагу, +карта.")
        elif kind == "martyr":
            d = (p.prestige + 1) // 2
            self._deal(opp, 0, pierce=d, source=p); p.prestige -= d
            self.log.append(f"{p.name} → «{nm}»: {d} в обход брони, −{d} себе.")

        # Este Muse: playing a blessed-element card restores 1 prestige
        if p.house == "este" and c["element"] == self.blessed and c["element"] != "neutral":
            p.prestige = min(START_PRESTIGE, p.prestige + 1)

        # move to discard
        p.hand.remove(c)
        p.discard.append(c)
        p.cards_played += 1

        self._clamp_resources()
        self._check_over()
        return None

    def _preview_spin(self):
        # show a likely next element (not authoritative — just a hint)
        return self.rng.choice(ELEMENTS)

    # ---- House relic activation ----
    def use_relic(self, seat):
        if seat != self.active: return "Сейчас не ваш ход."
        p = self.players[seat]
        opp = self.players[1 - seat]
        h = p.house
        if h == "medici":
            if p.relic_used_turn: return "Реликвия уже использована в этот ход."
            if p.florins < 3: return "Нужно 3 флорина."
            p.florins -= 3; p.draw(1, self.log); p.relic_used_turn = True
            self.log.append(f"{p.name}: Золотая книга → карта.")
        elif h == "sforza":
            if p.relic_used_turn: return "Реликвия уже использована в этот ход."
            if p.armor < 4: return "Нужно 4 брони."
            p.armor -= 4; self._deal(opp, 4, source=p); p.relic_used_turn = True
            self.log.append(f"{p.name}: Знамя капитана → 4 урона.")
        elif h == "este":
            if p.relic_used_turn: return "Реликвия уже использована в этот ход."
            p.florins += 1
            if p.has_status("poison"):
                p.add_status("poison", -1)
                self.log.append(f"{p.name}: Астролябия — снят 1 яд, +1 флорин.")
            else:
                self.peeked_next = self._preview_spin()
                self.log.append(f"{p.name}: Астролябия — {ELEMENT_RU.get(self.peeked_next,'?')} грядёт, +1 флорин.")
            p.relic_used_turn = True
        elif h == "borgia":
            if p.relic_used_game: return "Реликвия уже использована (раз за игру)."
            if not opp.has_status("poison"): return "На враге нет яда."
            opp.statuses["poison"] *= 2; p.relic_used_game = True
            self.log.append(f"{p.name}: Кантарелла удвоила яд до {opp.statuses['poison']}.")
        elif h == "savonarola":
            if p.relic_used_turn: return "Реликвия уже использована в этот ход."
            if p.prestige <= 2: return "Слишком мало престижа для жертвы."
            p.prestige -= 2; self._deal(opp, 0, pierce=4, source=p); p.relic_used_turn = True
            self.log.append(f"{p.name}: Власяница — −2 себе, 4 урона в обход брони.")
        self._clamp_resources()
        self._check_over()
        return None

    def take_debt(self, seat):
        if seat != self.active: return "Сейчас не ваш ход."
        p = self.players[seat]
        if p.debt: return "Долг уже взят."
        p.debt = True; p.florins += 3
        self._clamp_resources()
        self.log.append(f"{p.name} закладывает душу Фортуне: +3 флорина.")
        return None

    def respin(self, seat):
        if seat != self.active or not self.can_respin: return "Перекрутка недоступна."
        self.spin_wheel(); self.can_respin = False; self.peeked_next = None
        self.log.append(f"Колесо перекручено: {ELEMENT_RU[self.blessed]}.")
        return None

    def end_turn(self, seat):
        if seat != self.active: return "Сейчас не ваш ход."
        self.can_respin = False; self.peeked_next = None
        self.active = 1 - self.active
        self.start_turn()
        return None

    def _check_over(self):
        for p in self.players:
            if p.prestige <= 0:
                self.phase = "over"; self.winner = 1 - p.seat
                self.log.append(f"{self.players[self.winner].name} побеждает!")

    # ---- serialization ----
    def view_for(self, seat):
        out = {
            "phase": self.phase, "turn": self.turn, "active_seat": self.active,
            "blessed_element": self.blessed, "blessed_ru": ELEMENT_RU.get(self.blessed, ""),
            "blessed_lat": ELEMENT_LAT.get(self.blessed, ""), "wheel_angle": round(self.wheel_angle, 2),
            "you": seat, "can_respin": self.can_respin and seat == self.active,
            "peeked": self.peeked_next if seat == self.active else None,
            "peeked_ru": ELEMENT_RU.get(self.peeked_next, "") if (self.peeked_next and seat == self.active) else "",
            "winner": self.winner, "log": self.log[-8:], "players": [],
            "cards_per_turn": CARDS_PER_TURN,
            "compassion": self.compassion and seat == self.active,
        }
        for p in self.players:
            statuses = [{"id": s, "glyph": STATUS_GLYPH.get(s,"?"), "ru": STATUS_RU.get(s,s), "stacks": v}
                        for s, v in p.statuses.items() if v > 0]
            relic_ready = (not p.relic_used_turn) if p.house != "borgia" else (not p.relic_used_game)
            under_threat = self._lethal_threat(1 - p.seat, p.seat)
            pv = {
                "seat": p.seat, "name": p.name, "house": p.house,
                "house_name": HOUSES[p.house]["name"], "house_color": HOUSES[p.house]["color"],
                "avatar": p.avatar, "prestige": max(0, p.prestige), "armor": p.armor,
                "florins": p.florins, "hand_count": len(p.hand), "deck_count": len(p.deck),
                "discard_count": len(p.discard), "debt": p.debt, "statuses": statuses,
                "relic": HOUSES[p.house]["relic"], "relic_ready": relic_ready,
                "under_threat": under_threat, "cards_played": p.cards_played,
            }
            if p.seat == seat:
                hand = []
                for c in p.hand:
                    cc = dict(c)
                    cc["eff_cost"] = self._cost(p, c)
                    cc["eff_value"] = self._value(p, c)
                    cc["blessed"] = (c["element"] == self.blessed and c["element"] != "neutral")
                    hand.append(cc)
                pv["hand"] = hand
            out["players"].append(pv)
        return out


# ---------------------------------------------------------------------------
# Avatars — Met Museum API proxy (+ built-in SVG fallback handled client-side)
# ---------------------------------------------------------------------------
_avatar_cache = []
_avatar_lock = threading.Lock()
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# Curated Met object IDs of Renaissance-era portraits (public domain).
# These are fetched server-side; if offline, client uses SVG avatars.
MET_PORTRAIT_QUERY = "https://collectionapi.metmuseum.org/public/collection/v1/search?departmentId=11&dateBegin=1400&dateEnd=1600&hasImages=true&q=portrait"

def _met_fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "CasasFortunae/1.0"})
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=timeout) as r:
        return json.loads(r.read())

def load_avatars(limit=24):
    """Fetch a set of Renaissance portrait avatars from the Met. Returns list of
    {id, title, artist, date, image}. Cached. Safe to fail (returns [])."""
    global _avatar_cache
    with _avatar_lock:
        if _avatar_cache:
            return _avatar_cache
    try:
        search = _met_fetch(MET_PORTRAIT_QUERY)
        ids = search.get("objectIDs") or []
        random.shuffle(ids)
        out = []
        for oid in ids[:80]:
            if len(out) >= limit:
                break
            try:
                obj = _met_fetch(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", timeout=8)
            except Exception:
                continue
            img = obj.get("primaryImageSmall") or obj.get("primaryImage")
            if obj.get("isPublicDomain") and img:
                out.append({
                    "id": oid, "title": obj.get("title") or "Портрет",
                    "artist": obj.get("artistDisplayName") or "Неизвестный мастер",
                    "date": obj.get("objectDate") or "",
                    "image": img,
                })
        with _avatar_lock:
            _avatar_cache = out
        return out
    except Exception:
        return []

@app.route("/api/avatars")
def api_avatars():
    avs = load_avatars()
    return jsonify({"avatars": avs})


# ---------------------------------------------------------------------------
# Music — three classical tracks served from static/music
# ---------------------------------------------------------------------------
MUSIC_TRACKS = [
    {
        "id": "bach",
        "title": "Сюита для виолончели № 1",
        "subtitle": "Прелюдия · соль мажор",
        "composer": "И. С. Бах",
        "performer": "Ян Фоглер",
        "mood": "Созерцание",
        "file": "/static/music/track1_bach.mp3",
    },
    {
        "id": "haydn",
        "title": "Симфония № 55 «Школьный учитель»",
        "subtitle": "IV. Allegro · ми-бемоль мажор",
        "composer": "Й. Гайдн",
        "performer": "",
        "mood": "Бодрость",
        "file": "/static/music/track2_haydn.mp3",
    },
    {
        "id": "muti",
        "title": "Священное песнопение",
        "subtitle": "хоровое",
        "composer": "Риккардо Мути",
        "performer": "Шведский радиохор · Стокгольмский камерный хор",
        "mood": "Величие",
        "file": "/static/music/track3_muti.mp3",
    },
]

@app.route("/api/music")
def api_music():
    return jsonify({"tracks": MUSIC_TRACKS})

# Explicit, CWD-independent music route. Flask's default /static would also work,
# but launching from another folder can break it — this guarantees correct serving.
MUSIC_DIR = os.path.join(BASE_DIR, "static", "music")

@app.route("/static/music/<path:filename>")
def serve_music(filename):
    return send_from_directory(MUSIC_DIR, filename, mimetype="audio/mpeg", conditional=True)

def music_status():
    """Return (present, missing) lists of expected track files."""
    present, missing = [], []
    for t in MUSIC_TRACKS:
        fn = t["file"].split("/")[-1]
        (present if os.path.isfile(os.path.join(MUSIC_DIR, fn)) else missing).append(fn)
    return present, missing


# ---------------------------------------------------------------------------
# Backgrounds — Renaissance masterpieces from the Met (for the dynamic menu)
# ---------------------------------------------------------------------------
_bg_cache = []
_bg_lock = threading.Lock()

# Curated Met object IDs of dramatic Renaissance / Northern-Renaissance scenes
# (Bruegel, Bosch-school, large narrative paintings). Public domain, CC0.
# We try these first for quality, then fall back to a broad highlight search.
MET_MASTERPIECE_IDS = [
    435809,   # Bruegel — The Harvesters
    435844,   # Bruegel-circle landscape
    436101,   # large Renaissance scene
    437891,   # narrative panel
    459080,   # Northern Renaissance landscape
    435882,
    436573,
    435888,
]
MET_BG_QUERY = "https://collectionapi.metmuseum.org/public/collection/v1/search?departmentId=11&dateBegin=1400&dateEnd=1600&hasImages=true&isHighlight=true&q=landscape"

def load_backgrounds(limit=10):
    """Fetch dramatic Renaissance scene paintings for menu backgrounds.
    Returns list of {id, title, artist, image}. Cached. Safe to fail (returns [])."""
    global _bg_cache
    with _bg_lock:
        if _bg_cache:
            return _bg_cache
    out = []
    def consider(obj):
        img = obj.get("primaryImage") or obj.get("primaryImageSmall")
        if obj.get("isPublicDomain") and img:
            out.append({
                "id": obj.get("objectID"),
                "title": obj.get("title") or "Без названия",
                "artist": obj.get("artistDisplayName") or "Неизвестный мастер",
                "date": obj.get("objectDate") or "",
                "image": img,
            })
    try:
        # try curated masterpieces first
        for oid in MET_MASTERPIECE_IDS:
            if len(out) >= limit:
                break
            try:
                consider(_met_fetch(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", timeout=8))
            except Exception:
                continue
        # top up with highlight landscape search
        if len(out) < limit:
            search = _met_fetch(MET_BG_QUERY)
            ids = search.get("objectIDs") or []
            random.shuffle(ids)
            for oid in ids[:60]:
                if len(out) >= limit:
                    break
                try:
                    consider(_met_fetch(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", timeout=8))
                except Exception:
                    continue
        with _bg_lock:
            _bg_cache = out
        return out
    except Exception:
        return out

@app.route("/api/backgrounds")
def api_backgrounds():
    return jsonify({"backgrounds": load_backgrounds()})


# ---------------------------------------------------------------------------
# Rooms / lobby
# ---------------------------------------------------------------------------
class Room:
    def __init__(self, code):
        self.code = code
        self.players = {}        # token -> {name, seat, house, avatar, ready}
        self.sockets = {}        # token -> set(ws)
        self.state = None
        self.lock = threading.Lock()

    def seats_taken(self):
        return {info["seat"] for info in self.players.values()}

    def broadcast(self):
        if not self.state:
            payload = {
                "type": "lobby", "code": self.code,
                "players": [{"name": i["name"], "seat": i["seat"], "house": i.get("house"),
                             "avatar": i.get("avatar"), "ready": i.get("ready", False)}
                            for i in self.players.values()],
                "both_ready": len(self.players) >= 2 and all(i.get("ready") for i in self.players.values()),
            }
            for token, wss in list(self.sockets.items()):
                for ws in list(wss):
                    _safe_send(ws, payload)
            return
        for token, info in self.players.items():
            view = self.state.view_for(info["seat"])
            for ws in list(self.sockets.get(token, [])):
                _safe_send(ws, {"type": "state", "state": view})


rooms = {}
rooms_lock = threading.Lock()

def _safe_send(ws, obj):
    try:
        ws.send(json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass

def get_or_create_room(code):
    with rooms_lock:
        if code not in rooms:
            rooms[code] = Room(code)
        return rooms[code]

def new_code():
    with rooms_lock:
        while True:
            code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))
            if code not in rooms:
                return code


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("game.html")

@app.route("/api/houses")
def api_houses():
    return jsonify({"houses": list(HOUSES.values())})


@sock.route("/ws")
def ws_handler(ws):
    token = None
    room = None
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "hello":
                token = msg.get("token")
                name = (msg.get("name") or "Игрок").strip()[:16]
                code = (msg.get("room") or "MAIN").strip().upper()[:6] or "MAIN"
                room = get_or_create_room(code)
                with room.lock:
                    if token not in room.players:
                        if len(room.players) >= 2:
                            _safe_send(ws, {"type": "error", "message": "Комната заполнена."}); continue
                        taken = room.seats_taken()
                        seat = 0 if 0 not in taken else 1
                        room.players[token] = {"name": name, "seat": seat, "house": None, "avatar": None, "ready": False}
                    else:
                        room.players[token]["name"] = name
                    room.sockets.setdefault(token, set()).add(ws)
                    _safe_send(ws, {"type": "joined", "seat": room.players[token]["seat"], "room": room.code})
                    room.broadcast()

            elif mtype == "create_room":
                token = msg.get("token")
                name = (msg.get("name") or "Игрок").strip()[:16]
                code = new_code()
                room = get_or_create_room(code)
                with room.lock:
                    room.players[token] = {"name": name, "seat": 0, "house": None, "avatar": None, "ready": False}
                    room.sockets.setdefault(token, set()).add(ws)
                    _safe_send(ws, {"type": "joined", "seat": 0, "room": code})
                    room.broadcast()

            elif mtype == "choose":
                # player picks house + avatar and readies up
                if room is None or token is None: continue
                with room.lock:
                    if token in room.players:
                        if msg.get("house") in HOUSES:
                            room.players[token]["house"] = msg["house"]
                        if msg.get("avatar"):
                            room.players[token]["avatar"] = msg["avatar"]
                        room.players[token]["ready"] = bool(msg.get("ready"))
                    room.broadcast()

            elif mtype == "start":
                if room is None: continue
                with room.lock:
                    ready = len(room.players) >= 2 and all(i.get("ready") and i.get("house") for i in room.players.values())
                    if room.state is None and ready:
                        by_seat = {i["seat"]: i for i in room.players.values()}
                        p0 = by_seat.get(0); p1 = by_seat.get(1)
                        room.state = GameState(
                            {"name": p0["name"], "house": p0["house"], "avatar": p0.get("avatar") or {"type":"svg","id":0}},
                            {"name": p1["name"], "house": p1["house"], "avatar": p1.get("avatar") or {"type":"svg","id":1}},
                        )
                    room.broadcast()

            elif mtype in ("play_card", "take_debt", "respin", "end_turn", "use_relic", "request_state", "new_game"):
                if room is None or token is None: continue
                with room.lock:
                    if mtype == "new_game":
                        # reset to lobby for re-pick
                        room.state = None
                        for i in room.players.values():
                            i["ready"] = False
                        room.broadcast(); continue
                    if room.state is None:
                        room.broadcast(); continue
                    seat = room.players[token]["seat"]
                    err = None
                    if mtype == "play_card": err = room.state.play_card(seat, msg.get("uid"))
                    elif mtype == "take_debt": err = room.state.take_debt(seat)
                    elif mtype == "respin": err = room.state.respin(seat)
                    elif mtype == "end_turn": err = room.state.end_turn(seat)
                    elif mtype == "use_relic": err = room.state.use_relic(seat)
                    if err: _safe_send(ws, {"type": "error", "message": err})
                    room.broadcast()
    except Exception:
        pass
    finally:
        if room is not None and token is not None:
            with room.lock:
                if token in room.sockets:
                    room.sockets[token].discard(ws)


# ---------------------------------------------------------------------------
# LAN discovery + startup banner
# ---------------------------------------------------------------------------
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def print_banner(ip, port):
    url = f"http://{ip}:{port}"
    line = "═" * (len(url) + 8)
    print()
    print(f"  ╔{line}╗")
    print(f"  ║    {url}    ║")
    print(f"  ╚{line}╝")
    print()
    print("  CASAS FORTUNAE — Дома Фортуны")
    print("  ─────────────────────────────")
    print(f"  1. Оба телефона в одной WiFi-сети с этим компьютером.")
    print(f"  2. Открой на каждом телефоне:  {url}")
    print(f"  3. Введите имя, выберите Дом и портрет, нажмите «Готов».")
    print(f"  4. Когда оба готовы — начинается партия.")
    print()
    try:
        import qrcode
        qr = qrcode.QRCode(border=1); qr.add_data(url); qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("  ↑ Отсканируй QR-код вторым телефоном.")
        print()
    except Exception:
        print("  (Установи 'qrcode' для QR-кода: pip install qrcode)")
        print()
    # музыка: предупреждаем, если файлов нет
    present, missing = music_status()
    if missing:
        print("  ⚠ ВНИМАНИЕ: не найдены файлы музыки:")
        for fn in missing:
            print(f"      static/music/{fn}")
        print(f"  Положи их в папку:  {MUSIC_DIR}")
        print("  (Игра работает и без музыки — звуки боя остаются.)")
        print()
    else:
        print(f"  ♪ Музыка на месте: {len(present)} трека.")
        print()

# Prefetch avatars + backgrounds in background so menus are instant
def _prefetch():
    try: load_avatars()
    except Exception: pass
    try: load_backgrounds()
    except Exception: pass

if __name__ == "__main__":
    ip = lan_ip(); port = 5000
    print_banner(ip, port)
    threading.Thread(target=_prefetch, daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True)
