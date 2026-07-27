"""人设维度轴：在性格标签之外拉开「同 MBTI 不同人」的随机种子。"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

# 轴定义：稳定英文 key + 多语言展示
_AXIS_OPTIONS: Dict[str, List[Dict[str, Any]]] = {
    "career_class": [
        {"key": "student", "labels": {"zh": "学生/尚在求学", "en": "student", "ja": "学生", "ko": "학생", "pt": "estudante", "es": "estudiante", "id": "pelajar"}},
        {"key": "entry_white_collar", "labels": {"zh": "职场新人/基层白领", "en": "entry-level white collar", "ja": "若手会社員", "ko": "사회초년생", "pt": "iniciante corporativo", "es": "oficinista junior", "id": "karyawan pemula"}},
        {"key": "skilled_trade", "labels": {"zh": "技术工种/手艺人", "en": "skilled trade", "ja": "職人・現場職", "ko": "기술직", "pt": "ofício técnico", "es": "oficio técnico", "id": "pekerja terampil"}},
        {"key": "creative", "labels": {"zh": "创意/内容/设计", "en": "creative / content", "ja": "クリエイティブ", "ko": "크리에이티브", "pt": "criativo", "es": "creativo", "id": "kreatif"}},
        {"key": "tech", "labels": {"zh": "科技/工程", "en": "tech / engineering", "ja": "テック・工学", "ko": "테크/엔지니어", "pt": "tech/engenharia", "es": "tech/ingeniería", "id": "teknologi"}},
        {"key": "service_hospitality", "labels": {"zh": "服务/餐饮/零售", "en": "service / hospitality", "ja": "接客・サービス", "ko": "서비스업", "pt": "serviços", "es": "servicios", "id": "jasa/hospitality"}},
        {"key": "public_sector", "labels": {"zh": "体制内/公共部门", "en": "public sector", "ja": "公務員・公共", "ko": "공공/공직", "pt": "setor público", "es": "sector público", "id": "sektor publik"}},
        {"key": "entrepreneur", "labels": {"zh": "创业/自由职业", "en": "entrepreneur / freelance", "ja": "起業・フリー", "ko": "창업/프리랜서", "pt": "empreendedor", "es": "emprendedor", "id": "wirausaha"}},
        {"key": "finance_pro", "labels": {"zh": "金融/商务专业岗", "en": "finance / business pro", "ja": "金融・ビジネス", "ko": "금융/비즈니스", "pt": "finanças", "es": "finanzas", "id": "keuangan"}},
    ],
    "attachment": [
        {"key": "secure", "labels": {"zh": "安全型依恋", "en": "secure attachment", "ja": "安定型愛着", "ko": "안정 애착", "pt": "apego seguro", "es": "apego seguro", "id": "kelekatan aman"}},
        {"key": "anxious", "labels": {"zh": "焦虑型依恋", "en": "anxious attachment", "ja": "不安型愛着", "ko": "불안 애착", "pt": "apego ansioso", "es": "apego ansioso", "id": "kelekatan cemas"}},
        {"key": "avoidant", "labels": {"zh": "回避型依恋", "en": "avoidant attachment", "ja": "回避型愛着", "ko": "회피 애착", "pt": "apego evitante", "es": "apego evitativo", "id": "kelekatan menghindar"}},
        {"key": "fearful", "labels": {"zh": "恐惧-矛盾型", "en": "fearful-avoidant", "ja": "恐れ回避型", "ko": "공포-회피", "pt": "medo-evitativo", "es": "temeroso-evitativo", "id": "takut-menghindar"}},
    ],
    "conflict_style": [
        {"key": "direct", "labels": {"zh": "当面直说", "en": "direct confrontation", "ja": "面と向かって話す", "ko": "직접 말하기", "pt": "confronto direto", "es": "confrontación directa", "id": "langsung terang"}},
        {"key": "cool_down", "labels": {"zh": "先冷静再谈", "en": "cool down then talk", "ja": "冷めてから話す", "ko": "식힌 뒤 대화", "pt": "esfriar depois falar", "es": "enfriar y hablar", "id": "tenang dulu baru bicara"}},
        {"key": "compromise", "labels": {"zh": "折中求和", "en": "compromising", "ja": "妥協を探る", "ko": "타협 지향", "pt": "compromisso", "es": "compromiso", "id": "kompromi"}},
        {"key": "competitive", "labels": {"zh": "争赢/好辩", "en": "competitive / debate", "ja": "勝ちにこだわる", "ko": "승부형 논쟁", "pt": "competitivo", "es": "competitivo", "id": "kompetitif"}},
        {"key": "withdraw", "labels": {"zh": "沉默抽离", "en": "withdraw / stonewall", "ja": "黙って距離を取る", "ko": "침묵 거리두기", "pt": "afastar-se", "es": "retirarse", "id": "menarik diri"}},
    ],
    "life_pace": [
        {"key": "slow", "labels": {"zh": "慢节奏", "en": "slow pace", "ja": "スローペース", "ko": "느린 템포", "pt": "ritmo lento", "es": "ritmo lento", "id": "tempo lambat"}},
        {"key": "steady", "labels": {"zh": "稳健规律", "en": "steady routine", "ja": "安定したリズム", "ko": "규칙적 안정", "pt": "rotina estável", "es": "rutina estable", "id": "rutin stabil"}},
        {"key": "fast", "labels": {"zh": "快节奏高压", "en": "fast / high-pressure", "ja": "ハイペース", "ko": "빠른 고압", "pt": "ritmo acelerado", "es": "ritmo rápido", "id": "tempo cepat"}},
        {"key": "irregular", "labels": {"zh": "作息不规则", "en": "irregular schedule", "ja": "不規則", "ko": "불규칙", "pt": "horário irregular", "es": "horario irregular", "id": "jadwal tidak teratur"}},
    ],
    "interest_domain": [
        {"key": "sports", "labels": {"zh": "运动竞技", "en": "sports", "ja": "スポーツ", "ko": "스포츠", "pt": "esportes", "es": "deportes", "id": "olahraga"}},
        {"key": "arts", "labels": {"zh": "艺术审美", "en": "arts / aesthetics", "ja": "アート", "ko": "예술", "pt": "artes", "es": "artes", "id": "seni"}},
        {"key": "tech_gadgets", "labels": {"zh": "数码科技", "en": "tech & gadgets", "ja": "ガジェット", "ko": "디지털", "pt": "tech", "es": "gadgets", "id": "gadget"}},
        {"key": "food", "labels": {"zh": "美食烹饪", "en": "food & cooking", "ja": "食", "ko": "음식", "pt": "comida", "es": "comida", "id": "kuliner"}},
        {"key": "outdoors", "labels": {"zh": "户外自然", "en": "outdoors", "ja": "アウトドア", "ko": "아웃도어", "pt": "ar livre", "es": "aire libre", "id": "outdoor"}},
        {"key": "gaming", "labels": {"zh": "游戏电竞", "en": "gaming", "ja": "ゲーム", "ko": "게임", "pt": "games", "es": "videojuegos", "id": "game"}},
        {"key": "music", "labels": {"zh": "音乐现场", "en": "music", "ja": "音楽", "ko": "음악", "pt": "música", "es": "música", "id": "musik"}},
        {"key": "reading", "labels": {"zh": "阅读写作", "en": "reading / writing", "ja": "読書・執筆", "ko": "독서/글", "pt": "leitura", "es": "lectura", "id": "membaca"}},
        {"key": "markets", "labels": {"zh": "财经投资", "en": "markets / investing", "ja": "投資・経済", "ko": "재테크", "pt": "investimentos", "es": "inversiones", "id": "investasi"}},
        {"key": "travel", "labels": {"zh": "旅行探索", "en": "travel", "ja": "旅行", "ko": "여행", "pt": "viagem", "es": "viajes", "id": "travel"}},
    ],
}

AXIS_KEYS = tuple(_AXIS_OPTIONS.keys())


def _label(opt: Dict[str, Any], lang: str) -> str:
    labels = opt.get("labels") or {}
    return labels.get(lang) or labels.get("en") or opt["key"]


def list_axis_catalog(lang: str = "zh") -> Dict[str, List[Dict[str, str]]]:
    lang = (lang or "zh").split("-")[0]
    out: Dict[str, List[Dict[str, str]]] = {}
    for axis, opts in _AXIS_OPTIONS.items():
        out[axis] = [{"key": o["key"], "label": _label(o, lang)} for o in opts]
    return out


def sample_persona_axes(lang: str = "zh") -> Dict[str, Any]:
    """随机抽一套维度轴；返回 keys + 当地文案 + 可喂 prompt 的摘要行。"""
    lang = (lang or "zh").split("-")[0]
    keys: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    for axis, opts in _AXIS_OPTIONS.items():
        chosen = random.choice(opts)
        keys[axis] = chosen["key"]
        labels[axis] = _label(chosen, lang)
    return {
        "keys": keys,
        "labels": labels,
        "summary_zh": _format_axes_block(labels, "zh"),
        "summary": _format_axes_block(labels, lang),
    }


def resolve_axes_from_payload(data: dict, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """从请求体解析维度轴；缺省返回 None（由调用方决定是否再抽样）。"""
    import json
    lang = (lang or "zh").split("-")[0]
    raw_keys = data.get("persona_axes") or data.get("axes") or {}
    if isinstance(raw_keys, str):
        raw = raw_keys.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # 兼容 {"keys": {...}} 或扁平 {...}
                raw_keys = parsed.get("keys") if isinstance(parsed.get("keys"), dict) else parsed
            else:
                return None
        except Exception:
            return None
    keys: Dict[str, str] = {}
    if isinstance(raw_keys, dict):
        for axis in AXIS_KEYS:
            v = raw_keys.get(axis) or data.get(axis)
            if v:
                keys[axis] = str(v).strip()
    # 扁平字段兼容
    for axis in AXIS_KEYS:
        if axis not in keys and data.get(axis):
            keys[axis] = str(data.get(axis)).strip()
    if len(keys) < 3:
        return None
    labels: Dict[str, str] = {}
    for axis, key in keys.items():
        opts = _AXIS_OPTIONS.get(axis) or []
        match = next((o for o in opts if o["key"] == key), None)
        labels[axis] = _label(match, lang) if match else key
    # 补齐缺失轴
    for axis in AXIS_KEYS:
        if axis not in keys:
            opt = random.choice(_AXIS_OPTIONS[axis])
            keys[axis] = opt["key"]
            labels[axis] = _label(opt, lang)
    return {
        "keys": keys,
        "labels": labels,
        "summary": _format_axes_block(labels, lang),
        "summary_zh": _format_axes_block(labels, "zh"),
    }


def _format_axes_block(labels: Dict[str, str], lang: str) -> str:
    titles = {
        "zh": {
            "career_class": "职业阶层",
            "attachment": "依恋类型",
            "conflict_style": "冲突风格",
            "life_pace": "生活节奏",
            "interest_domain": "兴趣域",
        },
        "en": {
            "career_class": "Career class",
            "attachment": "Attachment",
            "conflict_style": "Conflict style",
            "life_pace": "Life pace",
            "interest_domain": "Interest domain",
        },
    }
    tmap = titles.get(lang, titles["en"])
    lines = [f"- {tmap.get(k, k)}: {labels.get(k, '')}" for k in AXIS_KEYS if labels.get(k)]
    return "\n".join(lines)


def axes_prompt_block(axes: Optional[Dict[str, Any]], lang: str = "zh") -> str:
    if not axes:
        return ""
    summary = axes.get("summary") or axes.get("summary_zh") or ""
    if not summary:
        return ""
    header = {
        "zh": "人设维度种子（必须消化进职业、亲密关系反应、日常节奏与爱好；拉开与同 MBTI 他人的差距，禁止只复述标签）：",
        "en": "Persona dimension seeds (must shape career, intimacy reactions, daily rhythm and hobbies; differentiate from others with the same MBTI):",
    }.get((lang or "zh").split("-")[0], None)
    if not header:
        header = "Persona dimension seeds (must be reflected in the persona; differentiate same-MBTI characters):"
    return f"\n{header}\n{summary}\n"
