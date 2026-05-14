"""
Stripe Checkout 自动支付模块
- 兑换 EFunCard 虚拟信用卡
- 填入 Stripe Checkout 表单
- 处理 hCaptcha (invisible) 和 3DS 验证
"""
import asyncio
import csv
import json
import os
import random
import re
import time
import requests
import urllib3
urllib3.disable_warnings()
from datetime import datetime
from playwright.async_api import async_playwright

EFUNCARD_API = "https://card.efuncard.com/api/external"
EFUNCARD_TOKEN = "b352d13f20462ed46cff0aa417065496bd811eb8396b2e2fee11aeacb796fc00"
CARD988_VERIFY_URL = "https://cards.779.chat/api/exchange/verify"


def log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] [{level.upper():5s}] {msg}')


def _apply_captcha_config(gemini_key=None, captcha_config=None):
    if captcha_config:
        if captcha_config.get("yescaptcha_key"):
            os.environ["YESCAPTCHA_API_KEY"] = captcha_config["yescaptcha_key"]
        if captcha_config.get("api_key"):
            os.environ["CAPTCHA_API_KEY"] = captcha_config["api_key"]
    elif gemini_key:
        os.environ["CAPTCHA_API_KEY"] = gemini_key


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _parse_expiry(value) -> tuple[str, str]:
    parts = re.findall(r"\d{1,4}", str(value or ""))
    if len(parts) < 2:
        raise ValueError("有效期格式错误，请使用 MM/YY 或 MM/YYYY")
    month, year = parts[0], parts[1]
    if len(month) == 4 and len(year) <= 2:
        year, month = month, year
    if not month.isdigit() or not 1 <= int(month) <= 12:
        raise ValueError("有效期月份无效")
    if len(year) == 2:
        year = f"20{year}"
    if len(year) != 4 or not year.isdigit():
        raise ValueError("有效期年份无效")
    return month.zfill(2), year


def normalize_card_info(card_info: dict) -> dict:
    """Normalize card information from GUI, JSON, file rows, or EFunCard."""
    card_number = _digits(card_info.get("cardNumber") or card_info.get("card_number") or card_info.get("number"))
    cvv = _digits(card_info.get("cvv") or card_info.get("cvc") or card_info.get("securityCode"))
    expiry_month = card_info.get("expiryMonth") or card_info.get("expiry_month") or card_info.get("month")
    expiry_year = card_info.get("expiryYear") or card_info.get("expiry_year") or card_info.get("year")

    if (not expiry_month or not expiry_year) and (card_info.get("expiry") or card_info.get("exp")):
        expiry_month, expiry_year = _parse_expiry(card_info.get("expiry") or card_info.get("exp"))

    expiry_month = str(expiry_month or "").zfill(2)
    expiry_year = str(expiry_year or "")
    if len(expiry_year) == 2:
        expiry_year = f"20{expiry_year}"

    if not card_number or len(card_number) < 12:
        raise ValueError("卡号无效")
    if not cvv or len(cvv) < 3:
        raise ValueError("CVV 无效")
    if not expiry_month.isdigit() or not 1 <= int(expiry_month) <= 12:
        raise ValueError("有效期月份无效")
    if not expiry_year.isdigit() or len(expiry_year) not in (2, 4):
        raise ValueError("有效期年份无效")

    billing_address = str(card_info.get("billingAddress") or card_info.get("billing_address") or "").strip()
    if not billing_address:
        address_parts = [
            card_info.get("addressLine1") or card_info.get("address") or "",
            card_info.get("city") or "",
            card_info.get("state") or "",
            card_info.get("postalCode") or card_info.get("zip") or "",
            card_info.get("country") or "US",
        ]
        billing_address = ", ".join(str(v).strip() for v in address_parts if str(v).strip())

    return {
        "cardNumber": card_number,
        "cvv": cvv,
        "expiryMonth": expiry_month,
        "expiryYear": expiry_year,
        "nameOnCard": str(card_info.get("nameOnCard") or card_info.get("name") or "Amy Allen").strip() or "Amy Allen",
        "billingAddress": billing_address,
        "status": card_info.get("status", "ACTIVE"),
    }


def _split_card_line(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    if "|" in text:
        return [p.strip() for p in text.split("|")]
    if "\t" in text:
        return [p.strip() for p in text.split("\t")]
    if "," in text:
        return [p.strip() for p in next(csv.reader([text]))]
    return [p.strip() for p in re.split(r"\s+", text) if p.strip()]


def parse_card_line(line: str) -> dict:
    """
    Parse one payment-card row.

    Supported formats:
      card|MM/YY|CVV|name|address|city|state|postal|country
      card|MM|YYYY|CVV|name|address|city|state|postal|country
      JSON object with cardNumber/cvv/expiryMonth/expiryYear fields
    """
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        raise ValueError("支付卡信息为空")
    if text.startswith("{"):
        return normalize_card_info(json.loads(text))

    parts = _split_card_line(text)
    if len(parts) < 3:
        raise ValueError("支付卡格式错误，至少需要: 卡号|有效期|CVV")

    if len(parts) >= 4 and re.fullmatch(r"\d{1,2}", parts[1]) and re.fullmatch(r"\d{2,4}", parts[2]):
        expiry_month, expiry_year = parts[1], parts[2]
        cvv = parts[3]
        extras = parts[4:]
    else:
        expiry_month, expiry_year = _parse_expiry(parts[1])
        cvv = parts[2]
        extras = parts[3:]

    name = extras[0] if extras else "Amy Allen"
    billing_address = ", ".join(v for v in extras[1:] if v)
    return normalize_card_info({
        "cardNumber": parts[0],
        "expiryMonth": expiry_month,
        "expiryYear": expiry_year,
        "cvv": cvv,
        "nameOnCard": name,
        "billingAddress": billing_address,
    })


def _parse_card988_address(address: str) -> dict:
    parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
    address_line1 = parts[0] if parts else ""
    city = ""
    postal_code = ""
    country = parts[-1] if len(parts) >= 3 else "US"
    if len(parts) >= 2:
        city_zip = parts[1]
        match = re.match(r"^(.*?)(?:\s+(\d{4,10}))?$", city_zip)
        if match:
            city = (match.group(1) or "").strip()
            postal_code = (match.group(2) or "").strip()
    return {
        "addressLine1": address_line1,
        "city": city,
        "postalCode": postal_code,
        "country": country or "US",
    }


def normalize_card988_content(content: dict) -> dict:
    """Convert card.988/cards.779 exchange response content to Stripe card info."""
    expiry_month, expiry_year = _parse_expiry(content.get("expiry_date") or content.get("expiry"))
    address_parts = _parse_card988_address(content.get("address", ""))
    card_info = {
        "cardNumber": content.get("card_number"),
        "expiryMonth": expiry_month,
        "expiryYear": expiry_year,
        "cvv": content.get("cvv"),
        "nameOnCard": content.get("name"),
        **address_parts,
        "smsApi": content.get("sms_api") or content.get("smsApi") or "",
        "phone": content.get("phone") or "",
    }
    return normalize_card_info(card_info) | {
        "smsApi": str(card_info.get("smsApi") or "").strip(),
        "phone": str(card_info.get("phone") or "").strip(),
    }


def card988_redeem(exchange_key: str, log=log) -> dict | None:
    """兑换 card.988/cards.779 key 获取虚拟卡和接码 API。"""
    key = str(exchange_key or "").strip()
    if not key:
        log("未填写 988 卡兑换 Key", "error")
        return None
    try:
        resp = requests.post(
            CARD988_VERIFY_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://cards.779.chat",
                "Referer": "https://cards.779.chat/",
            },
            json={"key": key},
            timeout=(10, 90),
            verify=False,
        )
        data = resp.json()
        if not data.get("success"):
            log(f"988 卡兑换失败: {data.get('message') or data.get('error') or data}", "error")
            return None
        content = data.get("content") or {}
        card_info = normalize_card988_content(content)
        card_meta = data.get("card") or {}
        log(f"988 卡兑换成功: *{card_info['cardNumber'][-4:]} ({card_meta.get('status', 'unknown')})", "ok")
        if card_info.get("phone"):
            log(f"接码手机号: {card_info['phone']}", "info")
        return card_info
    except Exception as e:
        log(f"988 卡兑换请求异常: {e}", "error")
        return None


def card988_get_sms(sms_api: str, log=log) -> dict | None:
    """轮询 card.988 返回的接码 API。"""
    url = str(sms_api or "").strip()
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=30, verify=False)
        text = resp.text.strip()
        if resp.status_code != 200 or not text:
            log(f"988 接码响应异常: HTTP {resp.status_code}", "warn")
            return None
        if "暂无验证码" not in text:
            segments = [part.strip() for part in re.split(r"[|,\n\r]+", text) if part.strip()]
            for segment in segments:
                if "到期时间" in segment:
                    continue
                match = re.search(r"(?:验证码|code|otp)[:：\s-]*(\d{4,8})", segment, re.I)
                if match:
                    return {"otp": match.group(1), "raw": text}
                if re.fullmatch(r"\d{4,8}", segment):
                    return {"otp": segment, "raw": text}
        log("988 暂无 3DS 验证码", "info")
        return None
    except Exception as e:
        log(f"988 接码查询异常: {e}", "warn")
        return None


def efun_redeem(code, log=log):
    """兑换 CDK 获取虚拟信用卡信息"""
    try:
        resp = requests.post(
            f"{EFUNCARD_API}/redeem",
            headers={
                "Authorization": f"Bearer {EFUNCARD_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"code": code},
            timeout=(10, 90),
            verify=False,
        )
        data = resp.json()
        if data.get("success"):
            card = data["data"]
            log(f"卡片兑换成功: *{card['lastFour']} ({card['status']})", "ok")
            log(f"  有效期至: {card.get('autoCancelAt', 'N/A')}", "info")
            return card
        else:
            log(f"兑换响应: {data.get('error')}", "warn")
            return None
    except Exception as e:
        log(f"兑换请求异常: {e}", "warn")
        return None


def efun_query(code, log=log):
    """查询已兑换卡片信息"""
    try:
        resp = requests.get(
            f"{EFUNCARD_API}/cards/query/{code}",
            headers={
                "Authorization": f"Bearer {EFUNCARD_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
            verify=False,
        )
        data = resp.json()
        if data.get("success"):
            return data["data"]
        log(f"查询响应: {data.get('error')}", "warn")
        return None
    except Exception as e:
        log(f"查询请求异常: {e}", "warn")
        return None


def efun_3ds_verify(code, minutes=5, log=log):
    """查询 3DS 验证码"""
    try:
        resp = requests.post(
            f"{EFUNCARD_API}/3ds/verify",
            headers={
                "Authorization": f"Bearer {EFUNCARD_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"code": code, "minutes": minutes},
            timeout=30,
            verify=False,
        )
        if resp.status_code != 200 or not resp.text.strip():
            log(f"3DS API 响应异常: HTTP {resp.status_code}, body='{resp.text[:100]}'", "warn")
            return None
        try:
            data = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            log(f"3DS API 返回非 JSON: '{resp.text[:100]}'", "warn")
            return None
        if data.get("success"):
            verifications = data["data"].get("verifications", [])
            if verifications:
                latest = verifications[0]
                log(f"3DS 验证码: {latest['otp']} (merchant: {latest.get('merchant', 'N/A')})", "ok")
                return latest
            log("暂无 3DS 验证码", "info")
            return None
        log(f"3DS 查询失败: {data.get('error')}", "error")
        return None
    except Exception as e:
        log(f"3DS 查询异常: {e}", "warn")
        return None


async def fill_stripe_checkout(payment_url, card_info, cdk_code=None, sms_api=None, log=log, headless=True):
    """
    自动填写 Stripe Checkout 表单并提交（无头模式）
    """
    card_info = normalize_card_info(card_info)
    card_number = card_info["cardNumber"]
    cvv = card_info["cvv"]
    expiry_month = str(card_info["expiryMonth"]).zfill(2)
    expiry_year = str(card_info["expiryYear"])[-2:]
    name_on_card = card_info.get("nameOnCard", "Amy Allen")
    billing_address = card_info.get("billingAddress", "") or card_info.get("nodeInstructions", "")

    addr_parts = [p.strip() for p in billing_address.split(",")]
    address_line1 = addr_parts[0] if len(addr_parts) > 0 else ""
    city = addr_parts[1] if len(addr_parts) > 1 else ""
    state = addr_parts[2] if len(addr_parts) > 2 else ""
    postal_code = addr_parts[3] if len(addr_parts) > 3 else ""
    country = addr_parts[4] if len(addr_parts) > 4 else "US"

    log(f"卡号: *{card_number[-4:]}, 有效期: {expiry_month}/{expiry_year}, 姓名: {name_on_card}")
    log(f"地址: {address_line1}, {city}, {state} {postal_code}, {country}")

    browser = None
    try:
        async with async_playwright() as p:
            from playwright_stealth import Stealth
            from kiro_register import _random_fingerprint_config, _build_fingerprint_script

            fp = _random_fingerprint_config()
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                f"--window-size={fp['screen']['width']},{fp['screen']['height']}",
            ]
            if headless:
                launch_args += ["--no-sandbox", "--disable-gpu"]

            browser = await p.chromium.launch(
                headless=headless,
                args=launch_args,
            )
            context = await browser.new_context(
                viewport=fp["viewport"],
                screen=fp["screen"],
                locale=fp["locale"],
                timezone_id=fp["timezone"],
                user_agent=fp["user_agent"],
                color_scheme="light",
                device_scale_factor=fp["pixel_ratio"],
            )
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
            await context.add_init_script(_build_fingerprint_script(fp))

            log("加载 Stripe 支付页面...")
            try:
                await page.goto(payment_url, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                log("页面加载失败，重试...", "warn")
                await asyncio.sleep(3)
                try:
                    await page.goto(payment_url, timeout=60000, wait_until="commit")
                except Exception:
                    log("支付页面无法加载", "error")
                    return {"ok": False, "status": "error", "message": "页面加载失败"}

            # 等待表单元素出现
            try:
                await page.wait_for_selector("#cardNumber", timeout=30000)
            except Exception:
                log("支付表单未加载，可能链接已失效", "error")
                return {"ok": False, "status": "error", "message": "支付表单未出现"}

            await asyncio.sleep(2)

            # 检查页面金额，非 $0 试用则中止
            log("检测试用状态: 读取今日应付金额...", "info")
            amount_value = None
            try:
                amount_text = await page.evaluate(r"""() => {
                    const body = document.body.innerText;
                    // 找 "Total due today" 或 "due today" 后面紧跟的金额(下一行)
                    const m = body.match(/(?:total due today|due today|amount due)\s*\n\s*\$([\d,.]+)/i);
                    if (m) return m[1];
                    // 找 "total" 后面紧跟的金额
                    const m2 = body.match(/\btotal\b\s*\n\s*\$([\d,.]+)/i);
                    if (m2) return m2[1];
                    return '';
                }""")
                if amount_text:
                    amount_value = float(amount_text.replace(",", ""))
                    log(f"页面金额: ${amount_text} (今日应付)", "info")
                    if amount_value > 0:
                        log(f"非 $0 试用 (${amount_text})，中止支付", "error")
                        await browser.close()
                        return {"ok": False, "status": "not_free_trial",
                                "message": f"今日应付 ${amount_text}，非免费试用"}
                    else:
                        log("今日应付 $0.00，确认为免费试用", "info")
                else:
                    log("未检测到 Total due today 金额，继续...", "warn")
            except Exception as e:
                log(f"金额检测异常: {e}", "warn")

            # 选择国家
            log("设置国家: United States")
            try:
                country_sel = page.locator("#billingCountry")
                if await country_sel.count() > 0:
                    await country_sel.select_option("US")
                    await asyncio.sleep(random.uniform(0.8, 1.5))
            except Exception:
                pass

            async def _stripe_move(loc):
                try:
                    box = await loc.bounding_box()
                    if box:
                        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                        await page.mouse.move(x, y, steps=random.randint(5, 12))
                        await asyncio.sleep(random.uniform(0.1, 0.3))
                except Exception:
                    pass

            async def _stripe_type(loc, text, delay_range=(40, 110)):
                await _stripe_move(loc)
                await loc.click()
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await loc.fill("")
                for i, ch in enumerate(text):
                    await page.keyboard.type(ch, delay=0)
                    d = random.uniform(delay_range[0], delay_range[1]) / 1000
                    if random.random() < 0.06:
                        d += random.uniform(0.15, 0.4)
                    await asyncio.sleep(d)
                await asyncio.sleep(random.uniform(0.4, 0.9))

            # 填写卡号
            log("填写卡号...")
            card_input = page.locator("#cardNumber")
            await _stripe_type(card_input, card_number, (45, 100))

            # 填写有效期
            log("填写有效期...")
            expiry_input = page.locator("#cardExpiry")
            await _stripe_type(expiry_input, f"{expiry_month}{expiry_year}", (50, 120))

            # 填写 CVV
            log("填写 CVV...")
            cvc_input = page.locator("#cardCvc")
            await _stripe_type(cvc_input, cvv, (60, 140))

            # 填写持卡人姓名
            log("填写持卡人姓名...")
            name_input = page.locator("#billingName")
            await _stripe_type(name_input, name_on_card, (35, 90))

            # 填写地址
            log("填写账单地址...")
            try:
                addr_input = page.locator("#billingAddressLine1")
                await _stripe_type(addr_input, address_line1, (30, 80))
            except Exception:
                pass

            try:
                postal_input = page.locator("#billingPostalCode")
                if await postal_input.count() > 0 and await postal_input.is_visible():
                    await _stripe_type(postal_input, postal_code, (50, 120))
            except Exception:
                pass

            try:
                city_input = page.locator("#billingLocality")
                if await city_input.count() > 0 and await city_input.is_visible():
                    await _stripe_type(city_input, city, (35, 90))
            except Exception:
                pass

            try:
                state_select = page.locator("#billingAdministrativeArea")
                if await state_select.count() > 0 and await state_select.is_visible():
                    try:
                        await state_select.select_option(state.strip())
                    except Exception:
                        try:
                            await state_select.fill(state.strip())
                        except Exception:
                            pass
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            log("表单填写完成，准备提交...", "ok")
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # 点击 Subscribe 按钮
            log("点击 Subscribe...")
            submit_btn = page.locator('button[type="submit"]')
            if await submit_btn.count() > 0:
                await _stripe_move(submit_btn)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await submit_btn.click()
            else:
                log("未找到提交按钮", "error")
                return {"ok": False, "status": "error", "message": "未找到提交按钮"}

            # 等待结果
            log("等待支付处理...")
            result = await _wait_for_payment_result(page, cdk_code, log, sms_api=sms_api)

            await browser.close()
            browser = None
            return result

    except Exception as e:
        err_msg = str(e)
        if "Target" in err_msg and "closed" in err_msg:
            log("浏览器意外关闭，支付中断", "error")
        elif "Timeout" in err_msg:
            log("操作超时", "error")
        else:
            log(f"支付流程异常: {err_msg[:100]}", "error")
        return {"ok": False, "status": "error", "message": err_msg[:100]}
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _wait_for_payment_result(page, cdk_code, log, timeout=120, sms_api=None):
    """等待支付结果，处理 hCaptcha 和 3DS"""
    start = time.time()
    manual_3ds_logged = False

    while time.time() - start < timeout:
        await asyncio.sleep(3)

        try:
            # 检查页面是否还活着
            current_url = page.url
        except Exception:
            log("页面已关闭", "error")
            return {"ok": False, "status": "error", "message": "页面意外关闭"}

        if "success" in current_url or "return_url" in current_url:
            log("支付成功! 页面已跳转", "ok")
            return {"ok": True, "status": "success", "url": current_url}

        try:
            page_text = await page.evaluate("() => document.body.innerText")
        except Exception:
            page_text = ""

        if "thank you" in page_text.lower() or "subscription active" in page_text.lower():
            log("支付成功! 检测到确认信息", "ok")
            return {"ok": True, "status": "success", "message": "subscription confirmed"}

        # hCaptcha 检测
        try:
            hcaptcha_visible = await page.evaluate("""() => {
                const iframe = document.querySelector('iframe[src*="hcaptcha.com/captcha"]');
                if (iframe && iframe.offsetWidth > 50 && iframe.offsetHeight > 50) return true;
                const challenge = document.querySelector('[data-hcaptcha-widget-id]');
                if (challenge && challenge.offsetWidth > 50) return true;
                return false;
            }""")
        except Exception:
            continue

        if hcaptcha_visible:
            log("检测到 hCaptcha，启动 YesCaptcha 求解...", "warn")
            try:
                from captcha_solver import solve_hcaptcha
                solved = await solve_hcaptcha(page, log_fn=log)
                if solved:
                    log("hCaptcha 求解成功!", "ok")
                else:
                    log("hCaptcha 求解失败", "error")
                    return {"ok": False, "status": "error", "message": "hCaptcha 求解失败"}
            except Exception:
                log("hCaptcha 处理异常", "error")
            continue

        # 3DS 检测
        try:
            is_3ds = await page.evaluate("""() => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                for (const f of iframes) {
                    if (f.src && (f.src.includes('3ds') || f.src.includes('acs') ||
                        f.src.includes('authenticate') || f.src.includes('challenge'))) {
                        return f.offsetWidth > 50;
                    }
                }
                const overlay = document.querySelector('[class*="3ds"], [class*="challenge"], [id*="3ds"]');
                return overlay && overlay.offsetWidth > 50;
            }""")
        except Exception:
            continue

        if is_3ds:
            log("检测到 3DS 验证!", "warn")
            if sms_api:
                try:
                    if await _handle_sms_3ds(page, sms_api, card988_get_sms, log):
                        sms_api = None
                except Exception:
                    log("988 3DS 处理异常", "error")
            elif cdk_code:
                try:
                    await _handle_3ds(page, cdk_code, log)
                except Exception:
                    log("3DS 处理异常", "error")
            elif not manual_3ds_logged:
                log("当前使用自定义支付卡，请在打开的浏览器窗口中手动完成 3DS 验证", "warn")
                manual_3ds_logged = True
            continue

        # 错误信息检测
        try:
            error_msg = await page.evaluate("""() => {
                const err = document.querySelector('[class*="error"], [class*="Error"], [role="alert"]');
                return err ? err.innerText.trim() : '';
            }""")
            if error_msg and len(error_msg) > 5:
                log(f"支付错误: {error_msg}", "error")
                return {"ok": False, "status": "error", "message": error_msg}
        except Exception:
            pass

        # 按钮状态
        try:
            btn_text = await page.evaluate("""() => {
                const btn = document.querySelector('button[type="submit"]');
                return btn ? btn.innerText.trim() : '';
            }""")
            if "processing" in btn_text.lower():
                log("处理中...", "dbg")
        except Exception:
            pass

    log("支付超时", "error")
    return {"ok": False, "status": "timeout"}


async def _handle_3ds(page, cdk_code, log):
    """处理 3DS 验证 - 从 EFunCard API 获取验证码并填入"""
    log("正在获取 3DS 验证码...", "info")

    # 轮询获取 3DS 验证码
    for attempt in range(10):
        await asyncio.sleep(5)
        verification = efun_3ds_verify(cdk_code, minutes=5, log=log)
        if verification:
            otp = verification["otp"]
            log(f"获取到 3DS OTP: {otp}", "ok")

            # 尝试在 3DS iframe 中填入验证码
            frames = page.frames
            for frame in frames:
                if frame == page.main_frame:
                    continue
                try:
                    otp_input = frame.locator('input[type="text"], input[type="tel"], input[name*="otp"], input[name*="code"], input[placeholder*="code"]')
                    if await otp_input.count() > 0:
                        await otp_input.first.fill(otp)
                        log("3DS 验证码已填入", "ok")
                        await asyncio.sleep(1)

                        # 点击提交按钮
                        submit = frame.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Verify")')
                        if await submit.count() > 0:
                            await submit.first.click()
                            log("3DS 验证已提交", "ok")
                        return
                except Exception:
                    continue

            # 如果没找到 iframe 内的输入框，尝试主页面
            try:
                otp_input = page.locator('input[name*="otp"], input[name*="code"], input[autocomplete*="one-time"]')
                if await otp_input.count() > 0:
                    await otp_input.first.fill(otp)
                    submit = page.locator('button[type="submit"]')
                    if await submit.count() > 0:
                        await submit.first.click()
                    log("3DS 验证码已在主页面填入并提交", "ok")
                    return
            except Exception:
                pass

            log("未找到 3DS 输入框，等待手动处理...", "warn")
            return

    log("3DS 验证码获取超时", "error")


async def _submit_3ds_otp(page, otp: str, log) -> bool:
    frames = page.frames
    for frame in frames:
        if frame == page.main_frame:
            continue
        try:
            otp_input = frame.locator('input[type="text"], input[type="tel"], input[name*="otp"], input[name*="code"], input[placeholder*="code"]')
            if await otp_input.count() > 0:
                await otp_input.first.fill(otp)
                log("3DS 验证码已填入", "ok")
                await asyncio.sleep(1)
                submit = frame.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Verify")')
                if await submit.count() > 0:
                    await submit.first.click()
                    log("3DS 验证已提交", "ok")
                return True
        except Exception:
            continue
    try:
        otp_input = page.locator('input[name*="otp"], input[name*="code"], input[autocomplete*="one-time"]')
        if await otp_input.count() > 0:
            await otp_input.first.fill(otp)
            submit = page.locator('button[type="submit"]')
            if await submit.count() > 0:
                await submit.first.click()
            log("3DS 验证码已在主页面填入并提交", "ok")
            return True
    except Exception:
        pass
    return False


async def _handle_sms_3ds(page, sms_api: str, sms_fetcher, log) -> bool:
    """处理从自定义接码 API 获取 OTP 的 3DS。"""
    log("正在通过接码 API 获取 3DS 验证码...", "info")
    for _ in range(10):
        await asyncio.sleep(5)
        verification = sms_fetcher(sms_api, log=log)
        if verification and verification.get("otp"):
            otp = verification["otp"]
            log(f"获取到 3DS OTP: {otp}", "ok")
            if await _submit_3ds_otp(page, otp, log):
                return True
            log("未找到 3DS 输入框，等待手动处理...", "warn")
            return False
    log("3DS 验证码获取超时", "error")
    return False


async def auto_pay(payment_url, cdk_code, gemini_key=None, captcha_config=None, headless=True, log=log):
    """
    完整自动支付流程:
    1. 兑换/查询虚拟信用卡
    2. 填写 Stripe 表单
    3. 处理验证并提交 (hCaptcha + 3DS)

    captcha_config: dict with keys: yescaptcha_key (推荐)
    """
    _apply_captcha_config(gemini_key=gemini_key, captcha_config=captcha_config)

    log("=" * 50, "ok")
    log("开始自动支付流程", "info")
    log("=" * 50, "ok")

    # Step 1: 获取卡片信息
    # 先查询是否已兑换且激活，避免重复兑换
    log("查询虚拟信用卡状态...")
    card_info = efun_query(cdk_code, log)

    if card_info and card_info.get("cardNumber") and card_info.get("status") == "ACTIVE":
        log(f"卡片已激活可用: *{card_info.get('lastFour', '????')}", "ok")
    else:
        # 未兑换或未激活，尝试兑换
        log("卡片未就绪，尝试兑换...")
        card_info = None
        for retry in range(3):
            card_info = efun_redeem(cdk_code, log)
            if card_info and card_info.get("cardNumber"):
                break
            if retry < 2:
                log(f"兑换未返回卡信息，等待 10s 后查询...", "info")
                time.sleep(10)
                card_info = efun_query(cdk_code, log)
                if card_info and card_info.get("cardNumber"):
                    break

        # 轮询等待卡片就绪
        if not card_info or not card_info.get("cardNumber"):
            log("轮询等待开卡...", "info")
            for attempt in range(18):
                time.sleep(10)
                log(f"查询卡片... ({(attempt+1)*10}s)", "info")
                card_info = efun_query(cdk_code, log)
                if card_info and card_info.get("cardNumber"):
                    break
            if not card_info or not card_info.get("cardNumber"):
                log("开卡超时，无法获取卡片信息!", "error")
                return None

        # 等待激活
        if card_info.get("status") and card_info["status"] != "ACTIVE":
            log(f"卡片状态: {card_info['status']}，等待激活...", "info")
            for attempt in range(12):
                time.sleep(5)
                card_info = efun_query(cdk_code, log)
                if card_info and card_info.get("status") == "ACTIVE":
                    log("卡片已激活!", "ok")
                    break
            else:
                if not card_info or card_info.get("status") != "ACTIVE":
                    log(f"卡片未能激活: {card_info.get('status') if card_info else 'None'}", "error")
                    return None

    # Step 2: 填写并提交
    result = await fill_stripe_checkout(payment_url, card_info, cdk_code, log=log, headless=headless)

    log("=" * 50, "ok")
    if result and result.get("ok"):
        log("支付流程完成!", "ok")
    else:
        log(f"支付流程结束: {result}", "warn")
    log("=" * 50, "ok")

    return result


async def auto_pay_with_card(payment_url, card_info, gemini_key=None, captcha_config=None, headless=False, log=log):
    """
    使用用户直接提供的支付卡信息完成 Stripe Checkout。
    3DS 验证需在打开的浏览器窗口中手动完成。
    """
    _apply_captcha_config(gemini_key=gemini_key, captcha_config=captcha_config)
    try:
        card_info = normalize_card_info(card_info)
    except Exception as e:
        log(f"支付卡信息无效: {e}", "error")
        return {"ok": False, "status": "invalid_card_info", "message": str(e)}

    log("=" * 50, "ok")
    log("开始自定义支付卡流程", "info")
    log("=" * 50, "ok")

    result = await fill_stripe_checkout(payment_url, card_info, cdk_code=None, log=log, headless=headless)

    log("=" * 50, "ok")
    if result and result.get("ok"):
        log("支付流程完成!", "ok")
    else:
        log(f"支付流程结束: {result}", "warn")
    log("=" * 50, "ok")
    return result


async def auto_pay_with_card988(payment_url, exchange_key, gemini_key=None, captcha_config=None, headless=True, log=log):
    """使用 988/779 兑换 Key 获取卡片并完成 Stripe Checkout。"""
    _apply_captcha_config(gemini_key=gemini_key, captcha_config=captcha_config)

    log("=" * 50, "ok")
    log("开始 988 卡自动兑换支付流程", "info")
    log("=" * 50, "ok")

    card_info = card988_redeem(exchange_key, log)
    if not card_info:
        return {"ok": False, "status": "card988_redeem_failed", "message": "988 卡兑换失败"}

    result = await fill_stripe_checkout(
        payment_url,
        card_info,
        cdk_code=None,
        sms_api=card_info.get("smsApi"),
        log=log,
        headless=headless,
    )

    log("=" * 50, "ok")
    if result and result.get("ok"):
        log("支付流程完成!", "ok")
    else:
        log(f"支付流程结束: {result}", "warn")
    log("=" * 50, "ok")
    return result


if __name__ == "__main__":
    import sys

    payment_url = sys.argv[1] if len(sys.argv) > 1 else 'https://checkout.stripe.com/c/pay/cs_live_b1F9f90pytQAzHaZSbHvc3xUeqcLAWaRrPEI9O7gQrwP8NZJzLOXKww0TO#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSd2cGd2ZndsdXFsamtQa2x0cGBrYHZ2QGtkZ2lgYSc%2FcXdwYCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxOYHwnPyd1blppbHNgWjA0V2pEUlJMTVBtcmFAa3dRRn1MSX9pYWlof3YyQURkf2o0bzdSTWhAT1J0X0NxZzFkYW5cN2dUcTNVTG41dmxJMTRtbG1OSlV2QXZuT300XU9zVUFUZE9dNTVkZFFoUzNBNScpJ2N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyY1YzVjNDUnKSdpZHxqcHFRfHVgJz8naHBpcWxabHFgaCcpJ2BrZGdpYFVpZGZgbWppYWB3dic%2FcXdwYHgl'
    cdk_code = sys.argv[2] if len(sys.argv) > 2 else "US-QV8Q4-CDEHM-GY7TU-PMDMR-R2JSA"

    captcha_cfg = {
        "yescaptcha_key": os.environ.get("YESCAPTCHA_API_KEY", ""),
    }

    asyncio.run(auto_pay(payment_url, cdk_code, captcha_config=captcha_cfg, headless=True))
