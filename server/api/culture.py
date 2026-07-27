from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.culture_data import (
    build_random_profile,
    get_cities,
    get_random_names,
    get_regions,
)
from services.persona_axes import list_axis_catalog
from services.personality_catalog import FEATURED_KEYS, MUTEX_PAIRS, all_personality_items
from services.region_catalog import find_region_for_city

router = APIRouter()


@router.get("/api/culture/names")
async def api_culture_names(
    lang: str = Query("zh", description="语言代码"),
    gender: str = Query("female", description="性别: male / female"),
    count: int = Query(5, ge=1, le=20, description="返回数量"),
):
    """获取符合当地文化的随机姓名列表"""
    names = get_random_names(lang, gender, count=count)
    return {"names": names, "lang": lang, "gender": gender}


@router.get("/api/culture/cities")
async def api_culture_cities(
    lang: str = Query("zh", description="语言代码"),
    region: Optional[str] = Query(None, description="地区 key，如 cn_mainland / us"),
    country: Optional[str] = Query(None, description="国家码，如 CN / US"),
):
    """获取城市列表；可按国家/地区过滤。无过滤时返回该语言全量。"""
    cities = get_cities(lang, region_key=region, country=country)
    return {"cities": cities, "lang": lang, "region": region, "country": country}


@router.get("/api/culture/regions")
async def api_culture_regions(
    lang: str = Query("zh", description="名字/文案语言圈"),
    ui_lang: Optional[str] = Query(None, description="地区标签展示语言，默认与 lang 相同"),
):
    """国家/地区 → 城市树；创建页先选地区再选城市。"""
    lang = (lang or "zh").split("-")[0]
    regions = get_regions(lang, ui_lang=ui_lang or lang)
    return {"lang": lang, "regions": regions}


@router.get("/api/culture/personalities")
async def api_culture_personalities(
    lang: str = Query("zh", description="语言代码"),
):
    """全量性格标签（稳定英文 key + 当地文案 + 分组/桶）；创建页推荐 featured_keys。"""
    lang = (lang or "zh").split("-")[0]
    items = all_personality_items(lang)
    return {
        "lang": lang,
        "items": items,
        "featured_keys": list(FEATURED_KEYS),
        "mutex_pairs": [list(p) for p in MUTEX_PAIRS],
        "groups": [
            {"id": "temperament", "label_zh": "气质"},
            {"id": "social", "label_zh": "社交"},
            {"id": "emotion", "label_zh": "情绪"},
            {"id": "style", "label_zh": "风格"},
        ],
    }


@router.get("/api/culture/persona-axes")
async def api_culture_persona_axes(
    lang: str = Query("zh", description="语言代码"),
):
    """人设维度轴可选值目录（职业阶层/依恋/冲突/节奏/兴趣域）。"""
    lang = (lang or "zh").split("-")[0]
    return {"lang": lang, "axes": list_axis_catalog(lang)}


@router.get("/api/culture/random-profile")
async def api_culture_random_profile(
    lang: str = Query("zh", description="语言代码"),
    gender: Optional[str] = Query(None, description="male / female / 男 / 女；缺省则随机"),
    sexual_orientation: Optional[str] = Query(None, description="可选固定性取向"),
    region: Optional[str] = Query(None, description="限定地区 key"),
    country: Optional[str] = Query(None, description="限定国家码"),
    city: Optional[str] = Query(None, description="锁定城市（有则优先用该城并反查 region）"),
):
    """真随机档案：含 country/region + persona_axes。"""
    lang = (lang or "zh").split("-")[0]
    g = gender
    if g and g not in ("male", "female", "男", "女"):
        raise HTTPException(status_code=400, detail="gender 须为 male/female 或 男/女")
    profile = build_random_profile(
        lang,
        gender=g,
        sexual_orientation=sexual_orientation,
        region_key=region,
        country=country,
        city=city,
    )
    tags = profile.get("personality_tags") or []
    if not tags and profile.get("personality"):
        raw = str(profile["personality"])
        tags = [p.strip() for p in raw.replace("，", "、").replace(",", "、").split("、") if p.strip()]
    region_meta = find_region_for_city(profile["city"], lang) or {}
    return {
        "lang": lang,
        "name": profile["name"],
        "age": profile["age"],
        "gender": profile["gender"],
        "city": profile["city"],
        "country": profile.get("country") or region_meta.get("country") or "",
        "region_key": profile.get("region_key") or region_meta.get("key") or "",
        "region_label": profile.get("region_label") or region_meta.get("label") or "",
        "mbti": profile["mbti"],
        "personality": tags,
        "personality_text": profile.get("personality") or "",
        "sexual_orientation": profile.get("sexual_orientation") or "heterosexual",
        "persona_axes": profile.get("persona_axes") or {},
        "persona_axes_labels": profile.get("persona_axes_labels") or {},
        "persona_axes_summary": profile.get("persona_axes_summary") or "",
    }
