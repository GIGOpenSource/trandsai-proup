"""性格标签目录：稳定英文 key + 多语言文案 + 抽样桶/分组/互斥。

用于创建页、批量生成、random-profile；展示用 label，存储可写「、」拼接的当地文案。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# bucket: soft（偏柔和，女向高频降权）| neutral | hard（硬朗/事业/竞技）
# group: temperament | social | emotion | style
_PERSONALITY_DEFS: List[Dict[str, Any]] = [
    # —— 中性保留 / 中性化 ——
    {"key": "calm", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "冷静", "en": "calm", "ja": "冷静", "ko": "침착", "pt": "calmo", "es": "calmado", "id": "tenang"}},
    {"key": "humorous", "bucket": "neutral", "group": "style",
     "labels": {"zh": "幽默", "en": "humorous", "ja": "ユーモラス", "ko": "유머러스", "pt": "bem-humorado", "es": "humorístico", "id": "lucu"}},
    {"key": "independent", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "独立", "en": "independent", "ja": "独立", "ko": "독립적", "pt": "independente", "es": "independiente", "id": "mandiri"}},
    {"key": "rational", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "理性", "en": "rational", "ja": "理性的", "ko": "이성적", "pt": "racional", "es": "racional", "id": "rasional"}},
    {"key": "straightforward", "bucket": "neutral", "group": "social",
     "labels": {"zh": "直率", "en": "straightforward", "ja": "率直", "ko": "솔직", "pt": "direto", "es": "directo", "id": "blak-blakan"}},
    {"key": "mature", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "成熟", "en": "mature", "ja": "成熟", "ko": "성숙", "pt": "maduro", "es": "maduro", "id": "dewasa"}},
    {"key": "cool", "bucket": "neutral", "group": "style",
     "labels": {"zh": "酷", "en": "cool", "ja": "クール", "ko": "쿨", "pt": "descolado", "es": "genial", "id": "keren"}},
    {"key": "artsy", "bucket": "neutral", "group": "style",
     "labels": {"zh": "文艺", "en": "artsy", "ja": "文芸的", "ko": "예술적", "pt": "artístico", "es": "artístico", "id": "artistik"}},
    {"key": "passionate", "bucket": "neutral", "group": "emotion",
     "labels": {"zh": "热情", "en": "passionate", "ja": "情熱的", "ko": "열정적", "pt": "apaixonado", "es": "apasionado", "id": "penuh gairah"}},
    {"key": "reserved", "bucket": "neutral", "group": "social",
     "labels": {"zh": "内敛", "en": "reserved", "ja": "内向的", "ko": "내향적", "pt": "reservado", "es": "reservado", "id": "tertutup"}},
    {"key": "sarcastic", "bucket": "neutral", "group": "style",
     "labels": {"zh": "毒舌", "en": "sarcastic", "ja": "毒舌", "ko": "독설", "pt": "sarcástico", "es": "sarcástico", "id": "sinis"}},
    {"key": "lazy", "bucket": "neutral", "group": "style",
     "labels": {"zh": "慵懒", "en": "laid-back", "ja": "のんびり", "ko": "느긋", "pt": "preguiçoso", "es": "perezoso", "id": "santai"}},
    {"key": "casual", "bucket": "neutral", "group": "style",
     "labels": {"zh": "随性", "en": "casual", "ja": "気まま", "ko": "자유로움", "pt": "casual", "es": "casual", "id": "santai-bebas"}},
    {"key": "rebellious", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "叛逆", "en": "rebellious", "ja": "反抗的", "ko": "반항적", "pt": "rebelde", "es": "rebelde", "id": "pemberontak"}},
    {"key": "elegant", "bucket": "neutral", "group": "style",
     "labels": {"zh": "优雅", "en": "elegant", "ja": "優雅", "ko": "우아", "pt": "elegante", "es": "elegante", "id": "anggun"}},
    {"key": "scheming", "bucket": "neutral", "group": "style",
     "labels": {"zh": "腹黑", "en": "scheming", "ja": "腹黒", "ko": "속이검은", "pt": "calculista", "es": "calculador", "id": "licik-halus"}},
    {"key": "sunny", "bucket": "soft", "group": "temperament",
     "labels": {"zh": "阳光", "en": "sunny", "ja": "元気", "ko": "밝음", "pt": "alegre", "es": "soleado", "id": "cerah"}},
    {"key": "lively", "bucket": "soft", "group": "social",
     "labels": {"zh": "活泼", "en": "lively", "ja": "活発", "ko": "활발", "pt": "energético", "es": "enérgico", "id": "ceria"}},
    {"key": "emotional", "bucket": "soft", "group": "emotion",
     "labels": {"zh": "感性", "en": "emotional", "ja": "感性的", "ko": "감성적", "pt": "emotivo", "es": "emotivo", "id": "emosional"}},
    {"key": "delicate", "bucket": "soft", "group": "emotion",
     "labels": {"zh": "细腻", "en": "sensitive", "ja": "繊細", "ko": "섬세", "pt": "delicado", "es": "delicado", "id": "halus"}},
    # —— 柔和池（女向降权/改名）——
    {"key": "gentle", "bucket": "soft", "group": "temperament",
     "labels": {"zh": "温柔", "en": "gentle", "ja": "優しい", "ko": "다정", "pt": "gentil", "es": "cariñoso", "id": "lembut"}},
    {"key": "cute", "bucket": "soft", "group": "style",
     "labels": {"zh": "可爱", "en": "cute", "ja": "可愛い", "ko": "귀여움", "pt": "fofo", "es": "adorable", "id": "imut"}},
    {"key": "affectionate", "bucket": "soft", "group": "emotion",
     "labels": {"zh": "重感情", "en": "affectionate", "ja": "情が深い", "ko": "정이많은", "pt": "afetuoso", "es": "afectuoso", "id": "penuh-perasaan"}},
    {"key": "mild", "bucket": "soft", "group": "temperament",
     "labels": {"zh": "温和", "en": "mild", "ja": "おとなしい", "ko": "온화", "pt": "ameno", "es": "suave", "id": "tenang-halus"}},
    {"key": "healing", "bucket": "soft", "group": "emotion",
     "labels": {"zh": "治愈", "en": "healing", "ja": "癒し系", "ko": "힐링", "pt": "acolhedor", "es": "sanador", "id": "menyembuhkan"}},
    {"key": "tsundere", "bucket": "soft", "group": "style",
     "labels": {"zh": "傲娇", "en": "tsundere", "ja": "ツンデレ", "ko": "츤데레", "pt": "tsundere", "es": "tsundere", "id": "tsundere"}},
    {"key": "yandere", "bucket": "soft", "group": "emotion",
     "labels": {"zh": "病娇", "en": "yandere", "ja": "ヤンデレ", "ko": "얀데레", "pt": "yandere", "es": "yandere", "id": "yandere"}},
    {"key": "commanding", "bucket": "hard", "group": "style",
     "labels": {"zh": "气场强", "en": "commanding", "ja": "存在感が強い", "ko": "카리스마", "pt": "imponente", "es": "imponente", "id": "berwibawa"}},
    {"key": "airheaded", "bucket": "soft", "group": "style",
     "labels": {"zh": "天然呆", "en": "airheaded", "ja": "天然", "ko": "천연", "pt": "distraído", "es": "distraído", "id": "polos"}},
    {"key": "mysterious", "bucket": "neutral", "group": "style",
     "labels": {"zh": "神秘", "en": "mysterious", "ja": "神秘的", "ko": "신비", "pt": "misterioso", "es": "misterioso", "id": "misterius"}},
    # —— 硬朗 / 中性新增 ——
    {"key": "steady", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "沉稳", "en": "steady", "ja": "落ち着き", "ko": "침착한", "pt": "sólido", "es": "sereno", "id": "tenang-teguh"}},
    {"key": "cheerful_blunt", "bucket": "hard", "group": "social",
     "labels": {"zh": "爽朗", "en": "bluntly cheerful", "ja": "爽やか", "ko": "시원시원", "pt": "descontraído", "es": "campechano", "id": "ceria-tegas"}},
    {"key": "taciturn", "bucket": "hard", "group": "social",
     "labels": {"zh": "寡言", "en": "taciturn", "ja": "口数が少ない", "ko": "과묵", "pt": "poucas palavras", "es": "de pocas palabras", "id": "pendiam"}},
    {"key": "protective", "bucket": "hard", "group": "emotion",
     "labels": {"zh": "护短", "en": "protective", "ja": "守りが強い", "ko": "챙김", "pt": "protetor", "es": "protector", "id": "protektif"}},
    {"key": "competitive", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "好胜", "en": "competitive", "ja": "負けず嫌い", "ko": "승부욕", "pt": "competitivo", "es": "competitivo", "id": "kompetitif"}},
    {"key": "reliable", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "可靠", "en": "reliable", "ja": "頼れる", "ko": "믿음직", "pt": "confiável", "es": "fiable", "id": "dapat-diandalkan"}},
    {"key": "hardcore", "bucket": "hard", "group": "style",
     "labels": {"zh": "硬核", "en": "hardcore", "ja": "ハードコア", "ko": "하드코어", "pt": "hardcore", "es": "hardcore", "id": "hardcore"}},
    {"key": "geeky", "bucket": "hard", "group": "style",
     "labels": {"zh": "极客", "en": "geeky", "ja": "ギーク", "ko": "긱", "pt": "nerd", "es": "friki", "id": "geek"}},
    {"key": "sporty", "bucket": "hard", "group": "style",
     "labels": {"zh": "运动", "en": "sporty", "ja": "スポーツ好き", "ko": "운동파", "pt": "esportivo", "es": "deportista", "id": "olahragawan"}},
    {"key": "ambitious", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "野心", "en": "ambitious", "ja": "野心的", "ko": "야심", "pt": "ambicioso", "es": "ambicioso", "id": "ambisius"}},
    {"key": "zen", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "佛系", "en": "zen", "ja": "淡々", "ko": "불계", "pt": "zen", "es": "zen", "id": "santai-pasrah"}},
    {"key": "fierce", "bucket": "hard", "group": "emotion",
     "labels": {"zh": "狠厉", "en": "fierce", "ja": "容赦ない", "ko": "매서운", "pt": "implacável", "es": "implacable", "id": "tegas-keras"}},
    {"key": "particular", "bucket": "neutral", "group": "style",
     "labels": {"zh": "讲究", "en": "particular", "ja": "こだわり", "ko": "까다로움", "pt": "exigente", "es": "exigente", "id": "teliti"}},
    {"key": "unbridled", "bucket": "hard", "group": "style",
     "labels": {"zh": "不羁", "en": "unbridled", "ja": "奔放", "ko": "자유분방", "pt": "indomável", "es": "indómito", "id": "liar-bebas"}},
    {"key": "social_butterfly", "bucket": "neutral", "group": "social",
     "labels": {"zh": "社牛", "en": "social butterfly", "ja": "社交的", "ko": "사교왕", "pt": "sociável", "es": "muy sociable", "id": "sosial"}},
    {"key": "social_anxious", "bucket": "neutral", "group": "social",
     "labels": {"zh": "社恐", "en": "socially anxious", "ja": "人見知り", "ko": "사회불안", "pt": "ansioso socialmente", "es": "ansioso social", "id": "cemas-sosial"}},
    {"key": "few_words", "bucket": "hard", "group": "social",
     "labels": {"zh": "话少", "en": "man of few words", "ja": "寡黙", "ko": "말수적음", "pt": "fala pouco", "es": "habla poco", "id": "sedikit-bicara"}},
    {"key": "meticulous", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "细心", "en": "meticulous", "ja": "細やか", "ko": "꼼꼼", "pt": "meticuloso", "es": "meticuloso", "id": "telaten"}},
    {"key": "carefree", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "大大咧咧", "en": "carefree", "ja": "おおらか", "ko": "털털", "pt": "despreocupado", "es": "despreocupado", "id": "cuek-baik"}},
    {"key": "slow_warm", "bucket": "neutral", "group": "emotion",
     "labels": {"zh": "慢热", "en": "slow-to-warm", "ja": "慣れにくい", "ko": "느린친화", "pt": "aquece devagar", "es": "se abre lento", "id": "lambat-hangat"}},
    {"key": "impulsive", "bucket": "hard", "group": "emotion",
     "labels": {"zh": "冲动", "en": "impulsive", "ja": "衝動的", "ko": "충동적", "pt": "impulsivo", "es": "impulsivo", "id": "impulsif"}},
    {"key": "neat_freak", "bucket": "neutral", "group": "style",
     "labels": {"zh": "洁癖", "en": "neat freak", "ja": "潔癖", "ko": "결벽", "pt": "maníaco por limpeza", "es": "maníaco de la limpieza", "id": "obsesi-bersih"}},
    {"key": "procrastinator", "bucket": "neutral", "group": "style",
     "labels": {"zh": "拖延", "en": "procrastinator", "ja": "先延ばし", "ko": "미루기", "pt": "procrastinador", "es": "procrastinador", "id": "penunda"}},
    {"key": "perfectionist", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "完美主义", "en": "perfectionist", "ja": "完璧主義", "ko": "완벽주의", "pt": "perfeccionista", "es": "perfeccionista", "id": "perfeksionis"}},
    {"key": "pragmatic", "bucket": "hard", "group": "temperament",
     "labels": {"zh": "务实", "en": "pragmatic", "ja": "実務的", "ko": "현실적", "pt": "pragmático", "es": "pragmático", "id": "pragmatis"}},
    {"key": "idealistic", "bucket": "soft", "group": "temperament",
     "labels": {"zh": "理想主义", "en": "idealistic", "ja": "理想主義", "ko": "이상주의", "pt": "idealista", "es": "idealista", "id": "idealis"}},
    {"key": "worldly", "bucket": "neutral", "group": "style",
     "labels": {"zh": "玩世", "en": "worldly", "ja": "シニカル", "ko": "세속적유머", "pt": "cínico leve", "es": "mundano", "id": "sinis-ringan"}},
    {"key": "loyal", "bucket": "neutral", "group": "emotion",
     "labels": {"zh": "忠诚", "en": "loyal", "ja": "一途", "ko": "충성", "pt": "leal", "es": "leal", "id": "setia"}},
    {"key": "freedom_loving", "bucket": "neutral", "group": "temperament",
     "labels": {"zh": "自由", "en": "freedom-loving", "ja": "自由を愛する", "ko": "자유를사랑", "pt": "ama liberdade", "es": "amante de la libertad", "id": "pecinta-kebebasan"}},
    {"key": "brotherly", "bucket": "hard", "group": "style",
     "labels": {"zh": "哥哥感", "en": "brotherly", "ja": "兄貴分", "ko": "형같은", "pt": "protetor fraterno", "es": "de hermano mayor", "id": "sosok-kakak"}},
    {"key": "youthful", "bucket": "neutral", "group": "style",
     "labels": {"zh": "少年感", "en": "youthful", "ja": "少年っぽい", "ko": "소년감", "pt": "juvenil", "es": "juvenil", "id": "remaja"}},
    {"key": "assertive", "bucket": "hard", "group": "social",
     "labels": {"zh": "有主见", "en": "assertive", "ja": "主張が強い", "ko": "자기주장", "pt": "assertivo", "es": "asertivo", "id": "tegas-pendirian"}},
    {"key": "warm_steady", "bucket": "neutral", "group": "emotion",
     "labels": {"zh": "暖而稳", "en": "warmly steady", "ja": "温かく安定", "ko": "따뜻단단", "pt": "quente e estável", "es": "cálido y firme", "id": "hangat-stabil"}},
]

# 高相关女向组合：避免同屏扎堆
MUTEX_PAIRS: List[Tuple[str, str]] = [
    ("yandere", "cute"),
    ("yandere", "commanding"),
    ("cute", "commanding"),
    ("social_butterfly", "social_anxious"),
    ("social_butterfly", "taciturn"),
    ("social_anxious", "cheerful_blunt"),
    ("impulsive", "steady"),
    ("zen", "ambitious"),
    ("zen", "competitive"),
]

FEATURED_KEYS: List[str] = [
    "calm", "humorous", "independent", "rational", "straightforward",
    "steady", "reliable", "cheerful_blunt", "sarcastic", "geeky",
    "sporty", "ambitious", "zen", "social_butterfly", "social_anxious",
    "slow_warm", "pragmatic", "loyal", "hardcore", "mature",
    "cool", "protective", "assertive", "warm_steady",
]


def _label(defn: Dict[str, Any], lang: str) -> str:
    labels = defn.get("labels") or {}
    return labels.get(lang) or labels.get("en") or defn["key"]


def all_personality_items(lang: str = "zh") -> List[Dict[str, Any]]:
    lang = (lang or "zh").split("-")[0]
    out = []
    for d in _PERSONALITY_DEFS:
        out.append({
            "key": d["key"],
            "label": _label(d, lang),
            "bucket": d["bucket"],
            "group": d["group"],
        })
    return out


def personality_label_map(lang: str = "zh") -> Dict[str, str]:
    return {d["key"]: _label(d, lang) for d in _PERSONALITY_DEFS}


def get_personality_labels_db(lang: str = "zh") -> List[str]:
    """兼容旧 PERSONALITIES_DB：当地文案列表。"""
    return [_label(d, lang) for d in _PERSONALITY_DEFS]


def _bucket_defs(bucket: str) -> List[Dict[str, Any]]:
    return [d for d in _PERSONALITY_DEFS if d["bucket"] == bucket]


def _conflicts(a: str, b: str) -> bool:
    for x, y in MUTEX_PAIRS:
        if (a == x and b == y) or (a == y and b == x):
            return True
    return False


def sample_personality_keys(
    count: int = 3,
    gender: Optional[str] = None,
) -> List[str]:
    """
    加权抽样 2～4 个 key。
    neutral 50% / soft 25% / hard 25%；gender 仅微调 soft/hard，不锁死性别。
    """
    import random

    count = max(2, min(int(count or 3), 4))
    g = (gender or "").strip().lower()
    # male/男 → soft 略降 hard 略升；female/女 相反；其它保持
    w_soft, w_neu, w_hard = 0.25, 0.50, 0.25
    if g in ("male", "男", "m"):
        w_soft, w_neu, w_hard = 0.18, 0.47, 0.35
    elif g in ("female", "女", "f"):
        w_soft, w_neu, w_hard = 0.32, 0.48, 0.20

    soft, neu, hard = _bucket_defs("soft"), _bucket_defs("neutral"), _bucket_defs("hard")
    picked: List[str] = []

    def pick_from(pool: Sequence[Dict[str, Any]]) -> Optional[str]:
        cand = [d for d in pool if d["key"] not in picked and not any(_conflicts(d["key"], p) for p in picked)]
        if not cand:
            cand = [d for d in pool if d["key"] not in picked]
        if not cand:
            return None
        return random.choice(cand)["key"]

    for _ in range(count):
        bucket = random.choices(
            ["soft", "neutral", "hard"],
            weights=[w_soft, w_neu, w_hard],
            k=1,
        )[0]
        pool = {"soft": soft, "neutral": neu, "hard": hard}[bucket]
        key = pick_from(pool)
        if key is None:
            # 任意剩余
            rest = [d["key"] for d in _PERSONALITY_DEFS if d["key"] not in picked]
            if not rest:
                break
            key = random.choice(rest)
        picked.append(key)
    return picked


def sample_personality_labels(
    lang: str = "zh",
    count: int = 3,
    gender: Optional[str] = None,
) -> List[str]:
    lang = (lang or "zh").split("-")[0]
    keys = sample_personality_keys(count=count, gender=gender)
    m = personality_label_map(lang)
    return [m.get(k, k) for k in keys]


def join_personality_labels(labels: List[str], lang: str = "zh") -> str:
    sep = "、" if (lang or "zh").startswith("zh") else ", "
    return sep.join(labels)
