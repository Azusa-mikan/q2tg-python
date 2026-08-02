from typing import Any

from telegram import Bot
from telegram.helpers import escape_markdown

_SUPER_FACE_PACKS = {
    "qlottie_2": "364 362 397 396 360 361 363 365 367".split(),  # noqa: SIM905
    "qlottie_3": "413 405 404 406 411 407 408 412 409".split(),  # noqa: SIM905
    "qlottie_4": "403 402 390 391 388 389 386 385 384 387".split(),  # noqa: SIM905
    "qlottie_5": "382 383 401 400 380 381 379 376 378 377".split(),  # noqa: SIM905
    "qlottie_6": "399 398 373 370 375 368 369 371 372 374".split(),  # noqa: SIM905
    "QQAniSticker": "5 311 312 319 320 339 137 346 344 345 181 74 75 349 350".split(),  # noqa: SIM905
    "qq_snake": "429 430 431_1 431_2 431_3 431_4 431_5 431_6 432".split(),  # noqa: SIM905
}
_SUPER_FACE_LOCATIONS = {
    face_id: (pack_name, index)
    for pack_name, face_ids in _SUPER_FACE_PACKS.items()
    for index, face_id in enumerate(face_ids)
}
_STICKER_PACK_CACHE: dict[tuple[int, str], tuple[str, ...]] = {}

OneBot_Face_Map: dict[str, int] = dict(
    zip(
        """
    0 1 10 100 101 102 103 104 105 106 107 108
    109 11 110 111 112 114 116 118 119 12 120 121
    123 124 125 129 13 137 14 144 146 147 15 16
    169 171 172 173 174 175 176 177 178 179 18 181
    182 183 185 187 19 2 20 201 21 212 22 23
    24 25 26 262 263 264 265 266 267 268 269 27
    270 271 272 273 277 28 281 282 283 284 285 286
    287 289 29 293 294 295 297 298 299 3 30 300
    302 303 305 306 307 31 311 312 314 317 318 319
    32 320 323 324 325 326 33 332 333 334 336 337
    338 339 34 341 342 343 344 345 346 347 349 35
    350 351 352 353 354 355 356 357 358 359 36 360
    361 362 363 364 365 366 367 368 369 37 370 371
    372 373 374 375 379 38 382 384 385 386 387 388
    389 39 390 391 392 393 394 395 396 397 398 399
    4 400 401 402 403 404 405 406 407 408 409 41
    410 411 412 413 415 416 417 419 42 420 421 422
    423 424 425 426 427 428 43 46 49 5 53 56
    59 6 60 63 64 66 67 7 74 75 76 77
    78 79 8 85 86 89 9 96 97 98 99
        """.split(),  # noqa: SIM905
        map(
            int,
            """
    3 5 7 9 11 13 15 17 19 21 23 25
    27 29 31 33 35 37 39 41 45 47 49 51
    53 55 57 59 61 63 65 67 69 71 73 75
    77 79 81 83 85 87 89 91 93 95 97 99
    101 103 105 107 109 111 113 115 117 119 121 123
    125 127 129 131 133 135 137 139 141 143 145 147
    149 151 153 155 157 159 161 163 165 167 169 171
    173 175 177 179 181 183 185 187 189 191 193 195
    197 199 201 203 205 207 209 211 213 215 217 219
    221 223 225 227 229 231 233 235 237 239 241 243
    245 247 249 251 253 255 257 259 261 263 265 267
    269 271 273 275 277 279 281 283 285 287 289 291
    293 295 297 299 301 303 305 307 309 311 313 315
    317 319 321 323 325 327 329 331 333 335 337 339
    341 343 345 347 349 351 353 355 357 359 361 364
    366 368 370 372 374 376 378 380 383 385 387 389
    391 393 395 397 399 401 403 405 407 409 411 413
    415 417 419 421 423 425 427 429 431 433 435 437
    439 441 443 445 447 449 451 453 455 457 459 461
    463 465 467 469 471 473 475 477 479 481 483
            """.split(),  # noqa: SIM905
        ),
        strict=True,
    )
)

OneBot_Face_Name_Map: dict[str, str] = dict(
    zip(
        """
    0 1 2 3 4 5 6 7 8 9 10 11
    12 13 14 15 16 18 19 20 21 22 23 24
    25 26 27 28 29 30 31 32 33 34 35 36
    37 38 39 41 42 43 46 49 53 56 59 60
    63 64 66 67 74 75 76 77 78 79 85 86
    89 96 97 98 99 100 101 102 103 104 105 106
    107 108 109 110 111 112 114 116 118 119 120 121
    123 124 125 129 137 144 146 147 169 171 172 173
    174 175 176 177 178 179 181 182 183 185 187 201
    212 262 263 264 265 266 267 268 269 270 271 272
    273 277 281 282 283 284 285 286 287 289 293 294
    295 297 298 299 300 302 303 305 306 307 311 312
    314 317 318 319 320 323 324 325 326 332 333 334
    336 337 338 339 341 342 343 344 345 346 347 349
    350 351 352 353 354 355 356 357 358 359 360 361
    362 363 364 365 366 367 368 369 370 371 372 373
    374 375 376 377 378 379 380 381 382 383 384 385
    386 387 388 389 390 391 392 393 394 395 396 397
    398 399 400 401 402 403 404 405 406 407 408 409
    410 411 412 413 415 416 417 419 420 421 422 423
    424 425 426 427 428 429 430 431 432
        """.split(),  # noqa: SIM905
        """
    /惊讶 /撇嘴 /色 /发呆 /得意 /流泪 /害羞 /闭嘴 /睡 /大哭 /尴尬 /发怒
    /调皮 /呲牙 /微笑 /难过 /酷 /抓狂 /吐 /偷笑 /可爱 /白眼 /傲慢 /饥饿
    /困 /惊恐 /流汗 /憨笑 /悠闲 /奋斗 /咒骂 /疑问 /嘘 /晕 /折磨 /衰
    /骷髅 /敲打 /再见 /发抖 /爱情 /跳跳 /猪头 /拥抱 /蛋糕 /刀 /便便 /咖啡
    /玫瑰 /凋谢 /爱心 /心碎 /太阳 /月亮 /赞 /踩 /握手 /胜利 /飞吻 /怄火
    /西瓜 /冷汗 /擦汗 /抠鼻 /鼓掌 /糗大了 /坏笑 /左哼哼 /右哼哼 /哈欠 /鄙视 /委屈
    /快哭了 /阴险 /左亲亲 /吓 /可怜 /菜刀 /篮球 /示爱 /抱拳 /勾引 /拳头 /差劲
    /NO /OK /转圈 /挥手 /鞭炮 /喝彩 /爆筋 /棒棒糖 /手枪 /茶 /眨眼睛 /泪奔
    /无奈 /卖萌 /小纠结 /喷血 /斜眼笑 /doge /戳一戳 /笑哭 /我最美 /羊驼 /幽灵 /点赞
    /托腮 /脑阔疼 /沧桑 /捂脸 /辣眼睛 /哦哟 /头秃 /问号脸 /暗中观察 /emm /吃瓜 /呵呵哒
    /我酸了 /汪汪 /无眼笑 /敬礼 /狂笑 /面无表情 /摸鱼 /魔鬼笑 /哦 /睁眼 /摸锦鲤 /期待
    /拿到红包 /拜谢 /元宝 /牛啊 /胖三斤 /左拜年 /右拜年 /右亲亲 /牛气冲天 /喵喵 /打call /变形
    /仔细分析 /菜汪 /崇拜 /比心 /庆祝 /嫌弃 /吃糖 /惊吓 /生气 /举牌牌 /烟花 /虎虎生威
    /豹富 /花朵脸 /我想开了 /舔屏 /打招呼 /酸Q /我方了 /大怨种 /红包多多 /你真棒棒 /大展宏兔 /坚强
    /贴贴 /敲敲 /咦 /拜托 /尊嘟假嘟 /耶 /666 /裂开 /骰子 /包剪锤 /亲亲 /狗狗笑哭
    /好兄弟 /狗狗可怜 /超级赞 /狗狗生气 /芒狗 /狗狗疑问 /奥特笑哭 /彩虹 /祝贺 /冒泡 /气呼呼 /忙
    /波波流泪 /超级鼓掌 /跺脚 /嗨 /企鹅笑哭 /企鹅流泪 /真棒 /路过 /emo /企鹅爱心 /晚安 /太气了
    /呜呜呜 /太好笑 /太头疼 /太赞了 /太头秃 /太沧桑 /龙年快乐 /新年中龙 /新年大龙 /略略略 /狼狗 /抛媚眼
    /超级ok /tui /快乐 /超级转圈 /别说话 /出去玩 /闪亮登场 /好运来 /姐是女王 /我听听 /臭美 /送你花花
    /么么哒 /一起嗨 /开心 /摇起来 /划龙舟 /中龙舟 /大龙舟 /火车 /中火车 /大火车 /粽于等到你 /复兴号
    /续标识 /求放过 /玩火 /偷感 /收到 /蛇年快乐 /蛇身 /蛇尾 /灵蛇献瑞
        """.split(),  # noqa: SIM905
        strict=True,
    )
)

def render_onebot_face(face_id: object) -> str:
    """把 OneBot face ID 渲染成 Telegram MarkdownV2。"""
    face_id = str(face_id)
    name = OneBot_Face_Name_Map.get(face_id, f"表情:{face_id}")
    label = escape_markdown(f"[{name}]", version=2)
    channel_message_id = OneBot_Face_Map.get(face_id)
    if channel_message_id is None:
        return label
    return f"[{label}](https://t.me/qq_face/{channel_message_id})"


def onebot_super_face_id(message: list[dict[Any, Any]]) -> str | None:
    """识别单独发送且已映射 Telegram Sticker 的 QQ 超级表情。"""
    if len(message) != 1:
        return None
    face = message[0]
    if face.get("type") != "face":
        return None
    data = face.get("data")
    if not isinstance(data, dict) or "id" not in data:
        return None
    face_id = str(data["id"])
    return face_id if face_id in _SUPER_FACE_LOCATIONS else None


def normalize_onebot_face_message(
    message: list[dict[Any, Any]],
) -> list[dict[Any, Any]]:
    """移除 OneBot 在单独超级表情后自动追加的同名 text 段。"""
    if len(message) != 2:
        return message
    face, text_segment = message
    face_data = face.get("data")
    text_data = text_segment.get("data")
    if (
        face.get("type") != "face"
        or text_segment.get("type") != "text"
        or not isinstance(face_data, dict)
        or not isinstance(text_data, dict)
        or "id" not in face_data
    ):
        return message
    name = OneBot_Face_Name_Map.get(str(face_data["id"]))
    if name is None or text_data.get("text") != f"[{name.removeprefix('/')}]":
        return message
    return [face]


async def onebot_super_face_file_id(bot: Bot, face_id: str) -> str | None:
    """从 Q2TG 对应 Telegram Sticker Pack 获取超级表情 file_id。"""
    location = _SUPER_FACE_LOCATIONS.get(face_id)
    if location is None:
        return None
    pack_name, index = location
    cache_key = (id(bot), pack_name)
    file_ids = _STICKER_PACK_CACHE.get(cache_key)
    if file_ids is None:
        sticker_set = await bot.get_sticker_set(pack_name)
        file_ids = tuple(sticker.file_id for sticker in sticker_set.stickers)
        _STICKER_PACK_CACHE[cache_key] = file_ids
    if index >= len(file_ids):
        return None
    return file_ids[index]
