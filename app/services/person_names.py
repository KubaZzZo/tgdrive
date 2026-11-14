from __future__ import annotations

import re
from functools import lru_cache
from unicodedata import normalize

import cmudict
from opencc import OpenCC

from app.services.people import person_key

# Common Chinese spellings take precedence over the approximate sound mapping.
COMMON_ENGLISH = {
    "alex": "亚历克斯",
    "alice": "爱丽丝",
    "anna": "安娜",
    "anthony": "安东尼",
    "ben": "本",
    "bob": "鲍勃",
    "brown": "布朗",
    "charles": "查尔斯",
    "chris": "克里斯",
    "connor": "康纳",
    "cruise": "克鲁斯",
    "david": "大卫",
    "doe": "多伊",
    "edward": "爱德华",
    "elizabeth": "伊丽莎白",
    "emma": "艾玛",
    "emily": "艾米莉",
    "george": "乔治",
    "jack": "杰克",
    "james": "詹姆斯",
    "jane": "简",
    "jason": "杰森",
    "jennifer": "詹妮弗",
    "john": "约翰",
    "johnson": "约翰逊",
    "julia": "朱莉娅",
    "kate": "凯特",
    "kevin": "凯文",
    "laura": "劳拉",
    "lily": "莉莉",
    "lisa": "丽莎",
    "lucy": "露西",
    "luna": "露娜",
    "mark": "马克",
    "mary": "玛丽",
    "michael": "迈克尔",
    "mike": "迈克",
    "miller": "米勒",
    "morgan": "摩根",
    "nielsen": "尼尔森",
    "olivia": "奥利维娅",
    "peter": "彼得",
    "robert": "罗伯特",
    "rose": "罗斯",
    "rowling": "罗琳",
    "sam": "山姆",
    "sarah": "莎拉",
    "smith": "史密斯",
    "sophie": "索菲",
    "swift": "斯威夫特",
    "taylor": "泰勒",
    "thomas": "托马斯",
    "tom": "汤姆",
    "watson": "沃森",
    "william": "威廉",
    "williams": "威廉姆斯",
    "wilson": "威尔逊",
}

LATIN_NAME = re.compile(r"[a-z]+(?:[ .·'’\-]+[a-z]+)*")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fc]+")
JAPANESE_FORM = re.compile(r"[々﨑髙沢辺桜広浜楽黒竜亀徳恵斎斉発絵実歳歩豊暁巌栄営穂児渋]")
HAN = re.compile(r"[\u3400-\u9fff\U00020000-\U000323af]")
HAN_VARIANTS = str.maketrans({"郞": "郎", "髙": "高", "﨑": "崎"})
VOWEL_COLUMN = {
    "AA": 0,
    "AE": 0,
    "AH": 1,
    "AO": 2,
    "AW": 3,
    "AY": 4,
    "EH": 5,
    "ER": 1,
    "EY": 5,
    "IH": 6,
    "IY": 6,
    "OW": 2,
    "OY": 7,
    "UH": 8,
    "UW": 8,
}
# The columns approximate the vowel groups in VOWEL_COLUMN. Each row is a
# consonant followed by a vowel. Standalone consonants cover clusters and codas.
SYLLABLES = {
    "": "阿 厄 奥 奥 艾 埃 伊 奥 乌",
    "B": "巴 伯 博 鲍 拜 贝 比 博 布",
    "CH": "查 彻 乔 乔 柴 切 奇 乔 丘",
    "D": "达 德 多 道 戴 戴 迪 多 杜",
    "F": "法 弗 佛 福 法 费 菲 福 福",
    "G": "加 格 戈 高 盖 盖 吉 戈 古",
    "HH": "哈 赫 霍 豪 海 黑 希 霍 胡",
    "JH": "贾 哲 乔 乔 杰 杰 吉 乔 朱",
    "K": "卡 克 科 考 凯 凯 基 科 库",
    "L": "拉 勒 洛 劳 莱 莱 利 洛 卢",
    "M": "马 默 莫 毛 麦 梅 米 莫 穆",
    "N": "纳 内 诺 瑙 奈 内 尼 诺 努",
    "P": "帕 珀 波 保 派 佩 皮 波 普",
    "R": "拉 勒 罗 劳 莱 雷 里 罗 鲁",
    "S": "萨 瑟 索 索 赛 塞 西 索 苏",
    "SH": "沙 舍 肖 绍 沙 谢 希 肖 舒",
    "T": "塔 特 托 陶 泰 泰 蒂 托 图",
    "TH": "萨 瑟 索 索 赛 塞 西 索 苏",
    "V": "瓦 弗 沃 沃 维 韦 维 沃 武",
    "W": "瓦 沃 沃 瓦 怀 韦 威 沃 乌",
    "Y": "亚 耶 约 约 亚 耶 伊 约 尤",
    "Z": "扎 泽 佐 佐 宰 泽 齐 佐 祖",
}
SYLLABLES = {onset: tuple(row.split()) for onset, row in SYLLABLES.items()}
CONSONANT = {
    "B": "布",
    "CH": "奇",
    "D": "德",
    "DH": "兹",
    "F": "夫",
    "G": "格",
    "HH": "赫",
    "JH": "吉",
    "K": "克",
    "L": "尔",
    "M": "姆",
    "N": "恩",
    "NG": "恩",
    "P": "普",
    "R": "尔",
    "S": "斯",
    "SH": "什",
    "T": "特",
    "TH": "斯",
    "V": "夫",
    "W": "沃",
    "Y": "伊",
    "Z": "兹",
    "ZH": "日",
}

KANA_ROWS = (
    ("アイウエオ", "阿 伊 乌 埃 奥"),
    ("カキクケコ", "卡 基 库 凯 科"),
    ("サシスセソ", "萨 希 苏 塞 索"),
    ("タチツテト", "塔 奇 茨 泰 托"),
    ("ナニヌネノ", "娜 妮 努 内 诺"),
    ("ハヒフヘホ", "哈 希 福 赫 霍"),
    ("マミムメモ", "玛 米 穆 梅 莫"),
    ("ヤユヨ", "亚 尤 约"),
    ("ラリルレロ", "拉 里 鲁 莱 罗"),
    ("ワヲン", "瓦 奥 恩"),
    ("ガギグゲゴ", "加 吉 古 盖 戈"),
    ("ザジズゼゾ", "扎 吉 兹 泽 佐"),
    ("ダヂヅデド", "达 吉 兹 德 多"),
    ("バビブベボ", "巴 比 布 贝 博"),
    ("パピプペポ", "帕 皮 普 佩 波"),
    ("ァィゥェォ", "阿 伊 乌 埃 奥"),
    ("ャュョヮヴヵヶ", "亚 尤 约 瓦 武 卡 凯"),
)
KANA_ZH = {
    kana: zh for row, values in KANA_ROWS for kana, zh in zip(row, values.split(), strict=True)
}
KANA_ZH.update(
    {
        "キャ": "恰",
        "キュ": "丘",
        "キョ": "京",
        "シャ": "莎",
        "シュ": "舒",
        "ショ": "肖",
        "チャ": "恰",
        "チュ": "丘",
        "チョ": "乔",
        "ジャ": "贾",
        "ジュ": "朱",
        "ジョ": "乔",
        "ティ": "蒂",
        "ディ": "迪",
        "ツァ": "察",
        "ファ": "法",
        "フィ": "菲",
        "フェ": "费",
        "フォ": "佛",
        "ウィ": "威",
        "ウェ": "韦",
        "ウォ": "沃",
        "ッ": "",
        "ー": "",
    }
)


@lru_cache(maxsize=1)
def _english_dictionary() -> dict[str, list[list[str]]]:
    return cmudict.dict()


@lru_cache(maxsize=1)
def _kanji_converters() -> tuple[OpenCC, OpenCC]:
    return OpenCC("jp2t.json"), OpenCC("t2s.json")


def _english_part(word: str) -> str | None:
    if word in COMMON_ENGLISH:
        return COMMON_ENGLISH[word]
    pronunciation = _english_dictionary().get(word)
    if not pronunciation:
        return None
    result: list[str] = []
    onset = ""
    for phone in pronunciation[0]:
        vowel = phone.rstrip("012")
        if vowel in VOWEL_COLUMN:
            row = SYLLABLES.get(onset)
            if row is None:
                result.append(CONSONANT[onset])
                row = SYLLABLES[""]
            result.append(row[VOWEL_COLUMN[vowel]])
            onset = ""
        else:
            if onset:
                result.append(CONSONANT[onset])
            onset = phone
    return "".join(result) + CONSONANT.get(onset, "")


def _kana_part(match: re.Match[str]) -> str:
    text = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in match[0])
    result: list[str] = []
    i = 0
    while i < len(text):
        pair = text[i : i + 2]
        if len(pair) == 2 and pair in KANA_ZH:
            result.append(KANA_ZH[pair])
            i += 2
        else:
            result.append(KANA_ZH.get(text[i], text[i]))
            i += 1
    return "".join(result) or match[0]


@lru_cache(maxsize=4096)
def chinese_person_name(name: str) -> str:
    """Produce a display label without changing the indexed original."""
    key = person_key(name)
    latin = "".join(ch for ch in normalize("NFKD", key) if not "\u0300" <= ch <= "\u036f")
    if LATIN_NAME.fullmatch(latin):
        words = re.split(r"[ .·\-]+", latin.replace("’", "'"))
        translated = [_english_part(word) for word in words]
        if all(translated):
            return "·".join(part for part in translated if part)
        return name
    has_japanese_form = bool(KANA.search(key) or JAPANESE_FORM.search(key))
    if not has_japanese_form and not HAN.search(key):
        return name
    japanese, simplified = _kanji_converters()
    label = KANA.sub(_kana_part, normalize("NFKC", name)).replace("・", "·")
    if has_japanese_form:
        label = re.sub(r"([\u3400-\u9fff])々", r"\1\1", label)
        label = japanese.convert(label).translate(HAN_VARIANTS)
    return simplified.convert(label)
