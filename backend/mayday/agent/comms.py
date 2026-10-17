"""Deterministic EN + Hindi stakeholder drafts (internal / merchant / customer). Nothing is sent."""
from __future__ import annotations


def _inr(paise: int) -> str:
    r = paise / 100
    return f"₹{r / 100000:.1f}L" if r >= 100000 else f"₹{r:,.0f}"


def draft_comms(rca: dict, incident: dict, action_executed: bool = False) -> list:
    cause, label = rca.get("cause", ""), rca.get("cause_label", "")
    blast = (incident or {}).get("blast", {})
    scope = blast.get("scope", "payments")
    pct = blast.get("failing_pct", 0)
    business = cause.startswith("business:")
    fixed_en = "A fix has been applied and success rates are recovering." if action_executed else "Our on-call team is mitigating."
    fixed_hi = "समाधान लागू कर दिया गया है और सफलता दर सुधर रही है।" if action_executed else "ऑन-कॉल टीम इसे ठीक कर रही है।"
    if business:
        internal_en = (f"[Internal] Payment success dip is user-side, not an outage: {label}. Infrastructure is healthy "
                       f"({scope}, ~{pct:.0f}% failing). No rollback performed. Suggest limit/campaign comms.")
        merchant_en = "Some customers are hitting their daily UPI limits during the current campaign. Payments systems are working normally."
        customer_en = "You may have reached your daily UPI limit. Try a smaller amount, another payment method, or retry tomorrow."
        internal_hi = f"[इंटरनल] भुगतान में गिरावट उपयोगकर्ता-स्तर की है, आउटेज नहीं: {label}। इंफ्रास्ट्रक्चर स्वस्थ है। रोलबैक नहीं किया।"
        merchant_hi = "इस कैम्पेन के दौरान कुछ ग्राहक अपनी दैनिक UPI सीमा पार कर रहे हैं। भुगतान सिस्टम सामान्य है।"
        customer_hi = "आपकी दैनिक UPI सीमा पूरी हो सकती है। कम राशि आज़माएं, दूसरा तरीका चुनें या कल पुन: प्रयास करें।"
    else:
        internal_en = (f"[Internal] Payment success dip: {label}. Scope: {scope}, ~{pct:.0f}% failing. {fixed_en} "
                       f"Evidence: {', '.join(e['id'] for e in rca.get('evidence', [])[:4])}.")
        merchant_en = f"Some payments are failing or slow due to an issue with {scope}. {fixed_en} We will update you shortly."
        customer_en = "Some payments may fail right now. Your money is safe; please retry in a few minutes."
        internal_hi = f"[इंटरनल] भुगतान सफलता में गिरावट: {label}। दायरा: {scope}, लगभग {pct:.0f}% विफल। {fixed_hi}"
        merchant_hi = f"{scope} में समस्या के कारण कुछ भुगतान विफल या धीमे हैं। {fixed_hi}"
        customer_hi = "अभी कुछ भुगतान विफल हो सकते हैं। आपका पैसा सुरक्षित है; कृपया कुछ मिनट बाद पुन: प्रयास करें।"
    return [
        {"audience": "internal", "lang": "en", "text": internal_en},
        {"audience": "merchant", "lang": "en", "text": merchant_en},
        {"audience": "customer", "lang": "en", "text": customer_en},
        {"audience": "internal", "lang": "hi", "text": internal_hi},
        {"audience": "merchant", "lang": "hi", "text": merchant_hi},
        {"audience": "customer", "lang": "hi", "text": customer_hi},
    ]
