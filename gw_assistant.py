"""
GW Assistant for Streamlit  (drop this file next to hh.py)

Adds a floating  chat assistant to the Streamlit dashboard.
It reads your active theme colours, knows your tabs / payment flow / required
columns, can jump to tabs, open the file uploader, and answers in
English / मराठी / Hinglish. No API key needed.
"""
import base64
import json
from io import BytesIO
from pathlib import Path

from PIL import Image
import streamlit as st
import streamlit.components.v1 as components

_JS = r"""
<script>
(function(){
 try {
  const CFG = __CFG__;
  const LOGO_DATA_URI = __LOGO_DATA_URI__;
  let P, D;
  try { P = window.parent; D = P.document; D.body; } catch(e){ return; }

  // remove any previous copy (Streamlit re-runs the script often)
  ["gwAsstRoot","gwAsstStyle"].forEach(id=>{const o=D.getElementById(id); if(o) o.remove();});
  const S = P.__gwAsst = P.__gwAsst || {hist:[], open:false, lang:"mr"};
  let lang = S.lang;

  // ---------- styles ----------
  const st = D.createElement("style"); st.id="gwAsstStyle";
  st.textContent = `
  #gwAsstRoot,#gwAsstRoot *{box-sizing:border-box;font-family:"Segoe UI",Arial,sans-serif}
  #gwAsstBtn{position:fixed;right:22px;bottom:22px;width:58px;height:58px;padding:0;border-radius:50%;
     display:flex;align-items:center;justify-content:center;overflow:hidden;
     background:${CFG.btn_bg};color:#fff;border:none;cursor:pointer;z-index:999999;box-shadow:0 8px 24px rgba(0,0,0,.4)}
  #gwAsstBtn img{display:block;width:100%;height:100%;object-fit:cover;border-radius:50%}
  #gwAsstBox{position:fixed;right:22px;bottom:92px;width:350px;max-width:calc(100vw - 30px);height:500px;
     max-height:calc(100vh - 120px);background:${CFG.panel};border:1px solid ${CFG.border};border-radius:16px;
     display:none;flex-direction:column;overflow:hidden;z-index:999999;box-shadow:0 20px 50px rgba(0,0,0,.5)}
  #gwAsstBox.open{display:flex}
  #gwAsstHead{background:${CFG.panel2};padding:12px 14px;display:flex;justify-content:space-between;align-items:center;
     color:${CFG.text};border-bottom:1px solid ${CFG.border}}
  #gwAsstHead b{font-size:14px;color:${CFG.text}} #gwAsstHead small{display:block;color:#22c55e;font-size:11px;margin-top:2px}
  #gwAsstX{background:transparent;color:${CFG.muted};font-size:22px;border:none;cursor:pointer;padding:0 4px}
  #gwAsstMsgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
  .gwa{max-width:88%;padding:9px 12px;border-radius:12px;font-size:13px;line-height:1.45;white-space:pre-wrap}
  .gwa.bot{background:${CFG.panel2};color:${CFG.text};border:1px solid ${CFG.border};align-self:flex-start;border-bottom-left-radius:3px}
  .gwa.me{background:${CFG.btn_bg};color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
  #gwAsstChips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px}
  #gwAsstChips button{background:transparent;color:${CFG.accent};border:1px solid ${CFG.border};border-radius:14px;
     padding:5px 10px;font-size:11px;font-weight:700;cursor:pointer}
  #gwAsstChips button:hover{background:${CFG.panel2}}
  #gwAsstForm{display:flex;gap:8px;padding:10px;border-top:1px solid ${CFG.border};background:${CFG.panel}}
  #gwAsstIn{flex:1;background:${CFG.panel2};color:${CFG.text};border:1px solid ${CFG.border};border-radius:8px;padding:9px 10px;font-size:13px;outline:none}
  #gwAsstSend{background:${CFG.btn_bg};color:#fff;border:none;border-radius:8px;padding:0 14px;font-weight:700;cursor:pointer}
  `;
  D.head.appendChild(st);

  // ---------- markup ----------
  const root = D.createElement("div"); root.id="gwAsstRoot";
  root.innerHTML = `
   <button id="gwAsstBtn" type="button" aria-label="Open GW Analytics Assistant" title="GW Assistant">
    <img src="${LOGO_DATA_URI}" alt="GW Assistant">
   </button>
    <div id="gwAsstBox" role="dialog" aria-label="GW Assistant">
      <div id="gwAsstHead"><div><b>GW Assistant</b><small></small></div>
        <button id="gwAsstX" type="button" aria-label="Close">×</button></div>
      <div id="gwAsstMsgs"></div><div id="gwAsstChips"></div>
      <div id="gwAsstForm"><input id="gwAsstIn" type="text" placeholder="How can I help you today??" autocomplete="off">
        <button id="gwAsstSend" type="button">Send</button></div>
    </div>`;
  D.body.appendChild(root);
  const $ = id => D.getElementById(id);
  const msgs=$("gwAsstMsgs"), chips=$("gwAsstChips"), input=$("gwAsstIn"), box=$("gwAsstBox");

  // ---------- knowledge ----------
  const COLS = "date, product, quantity, selling_price, cost_price";
  const OPT  = "discount, customer, category, region, order_id, operating_expense, cost_type (Fixed/Variable)";
  const T = {
    hello:["Hi! 👋 I'm the GW assistant. I can explain the dashboard tabs, file format, forecasting, payment and PDFs. What do you need?",
           "Namaskar! 👋 Mi GW assistant aahe. Dashboard tabs, file format, forecast, payment ani PDF baddal madat karto. Kay pahije?"],
    what:["This is the GW Sales Intelligence Suite: upload a sales file and get KPIs, sales/cost/profit charts, product & customer analytics, a 12-month AI forecast, risk alerts and a downloadable PDF report.",
          "Hi GW Sales Intelligence Suite aahe: sales file upload kara ani KPIs, sales/cost/profit charts, product ani customer analytics, 12 mahinyanche AI forecast, risk alerts ani PDF report miltat."],
    upload:["Upload CSV / Excel (.xlsx) / XML in the sidebar (left). Without a file you see demo data.\nRequired columns: "+COLS+".\nOptional: "+OPT+".\nColumn names are case-insensitive; spaces/dashes become underscores.",
            "Sidebar (dava) madhe CSV / Excel (.xlsx) / XML upload kara. File nasel tar demo data distoy.\nAvashyak columns: "+COLS+".\nOptional: "+OPT+".\nColumn names capital/small chalatat; space/dash la underscore samjtat."],
    missing:["“Required columns are missing” means your file lacks one of: "+COLS+". Rename your headers (e.g. 'Price' → selling_price, 'Cost' → cost_price) and upload again.",
             "“Required columns are missing” mhanje file madhe yapaiki ek column nahi: "+COLS+". Headers che nav badla (udaharan 'Price' → selling_price, 'Cost' → cost_price) ani punha upload kara."],
    tabs:["Tabs: Sales • Cost • Profit & Loss • Products • Customers • Time • Growth • Forecast • Risk & Alerts • Comprehensive AI Report • Pro Subscription & Receipt. Tap a button below to jump.",
          "Tabs: Sales • Cost • Profit & Loss • Products • Customers • Time • Growth • Forecast • Risk & Alerts • Comprehensive AI Report • Pro Subscription & Receipt. Khali button dabun tithe jaa."],
    forecast:["The Forecast tab predicts the next 12 months of sales with a Random Forest model. It needs at least 6 months of data in the selected date range.",
              "Forecast tab Random Forest model ne pudhche 12 mahine che sales predict karto. Nivadlelya date range madhe kamit kami 6 mahinyancha data pahije."],
    risk:["Risk & Alerts lists loss-making products and low-margin items (below 10%).",
          "Risk & Alerts madhe tote deNare products ani kami margin (10% peksha kami) che items disatat."],
    filters:["Use the sidebar filters: Date Range, Year, Category and Product. If you see “No data for selected filters”, widen the date range or re-select categories/products.",
             "Sidebar filters vapra: Date Range, Year, Category, Product. “No data for selected filters” disle tar date range vadhva kinva category/product punha nivada."],
    theme:["Change the colour theme from the sidebar → “Select Color Theme” (Midnight Blue, Crisp White, Emerald Forest, Royal Purple).",
           "Sidebar → “Select Color Theme” madhun theme badla (Midnight Blue, Crisp White, Emerald Forest, Royal Purple)."],
    pay:["Report and receipt PDFs need the Pro plan (₹10, 1 month). Log in, open “💳 Pro Subscription & Receipt”, tap “Pay ₹10 securely”, complete the payment, then come back and tap “Refresh status”. PDFs unlock only after the server verifies the payment.",
         "Report ani receipt PDF sathi Pro plan (₹10, 1 mahina) lagto. Login kara, “💳 Pro Subscription & Receipt” ughada, “Pay ₹10 securely” dabva, payment kara, mag parat yeun “Refresh status” dabva. Server payment verify kelyavarach PDF unlock hoto."],
    utr:["The UTR is the 12-digit transaction reference shown in your UPI app under payment details after you pay by QR.",
         "UTR mhanje QR ne pay kelyavar UPI app madhe payment details madhe distaNara 12-digit transaction reference."],
    receipt:["After payment, the same tab shows your official receipt with a “Download Official Receipt (PDF)” button.",
             "Payment nantar tya-ch tab madhe official receipt disate ani “Download Official Receipt (PDF)” button yeto."],
    report:["Open the “📑 Comprehensive AI Report” tab for statistics, 4 charts with explanations and recommendations. The full PDF download unlocks after payment.",
            "“📑 Comprehensive AI Report” tab ughada: statistics, 4 charts explanation sahit ani recommendations miltat. Purna PDF payment nantar download hoto."],
    login:["Login or register at 127.0.0.1:5000/login.html. The sidebar shows your account and subscription status once logged in.",
           "127.0.0.1:5000/login.html var login kinva register kara. Login nantar sidebar madhe account ani subscription status disto."],
    fail:["Something not working? Check: 1) required columns exist 2) dates are valid (date column parses) 3) filters aren't excluding everything 4) Flask API on port 5000 is running for login/payment. Tell me exactly what you see.",
          "Kahi chalat nahi ka? Tapasa: 1) required columns aahet ka 2) date column barobar aahe ka 3) filters sagla data kadhat nahi na 4) login/payment sathi port 5000 var Flask chalu aahe ka. Nakki kay disat aahe te sanga."],
    thanks:["You're welcome! 😊","Dhanyavaad! 😊 Kadhihi vichara."],
    bye:["Bye! 👋","Bye! Chhan divas jao 👋"],
    unknown:["I'm not sure about that yet. I can help with file format, tabs, forecast, filters, themes, payment, receipt and PDFs. Try a button below.",
             "He mala ajun samajle nahi. File format, tabs, forecast, filters, theme, payment, receipt ani PDF baddal madat karu shakto. Khalil button try kara."]
  };
  const L=(en,mr)=>lang==="en"?en:mr;
  const say=k=>T[k][lang==="en"?0:1];

  function statusText(){
    const parts=[];
    parts.push(L("Dataset: ","Dataset: ")+CFG.dataset+" ("+CFG.rows.toLocaleString()+L(" rows after filters)"," rows, filters nantar)"));
    parts.push(CFG.user ? L("Logged in as ","Login: ")+CFG.user : L("Not logged in","Login kelele nahi"));
    parts.push(CFG.paid ? L("Pro plan: ACTIVE ✅ (PDFs unlocked)","Pro plan: ACTIVE ✅ (PDF unlock aahet)")
                        : L("Pro plan: not active 🔒 (PDFs locked)","Pro plan: active nahi 🔒 (PDF locked aahet)"));
    return parts.join("\n");
  }

  // ---------- helpers ----------
  function add(text,who,save=true){
    const d=D.createElement("div"); d.className="gwa "+who; d.textContent=text; msgs.appendChild(d); msgs.scrollTop=msgs.scrollHeight;
    if(save){S.hist.push({w:who,t:text}); if(S.hist.length>60) S.hist.shift();}
  }
  function setChips(list){chips.innerHTML="";list.forEach(([label,fn])=>{const b=D.createElement("button");b.type="button";b.textContent=label;
    b.onclick=()=>{add(label,"me");fn();};chips.appendChild(b);});}
  function goTab(key){
    const tabs=[...D.querySelectorAll('button[role="tab"]')];
    const t=tabs.find(b=>b.textContent.toLowerCase().includes(key.toLowerCase()));
    if(t){t.click(); t.scrollIntoView({behavior:"smooth",block:"center"}); add(L("Opened: ","Ughadle: ")+t.textContent.trim(),"bot"); setOpen(false);}
    else add(L("I couldn't find that tab on screen.","Ha tab screen var sapadla nahi."),"bot");
  }
  function openUploader(){
    const b=D.querySelector('[data-testid="stFileUploaderDropzone"] button');
    if(b){setOpen(false);b.click();}
    else add(L("Open the sidebar (arrow at top-left) to see the uploader.","Uploader pahnyasathi sidebar ughada (varchya dava konyatil arrow)."),"bot");
  }
  const A={
    tab:k=>()=>goTab(k), upload:openUploader,
    login:()=>{P.location.href="http://127.0.0.1:5000/login.html";}
  };

  function mainChips(){ setChips([
    [L("What is this?","He kay aahe?"),()=>reply("what")],
    [L("Upload / file format","Upload / file format"),()=>reply("upload")],
    [L("Tabs","Tabs"),()=>reply("tabs")],
    [L("Forecast","Forecast"),()=>reply("forecast")],
    [L("Pay ₹10 / PDF","₹10 payment / PDF"),()=>reply("pay")],
    [L("My status","Maza status"),()=>{add(statusText(),"bot");mainChips();}]
  ]);}
  function reply(key){
    add(say(key),"bot");
    const back=["⬅ Back",mainChips];
    if(key==="pay") setChips([[L("Open payment tab","Payment tab ughada"),A.tab("Pro Subscription")],[L("What is UTR?","UTR mhanje?"),()=>reply("utr")],back]);
    else if(key==="receipt") setChips([[L("Open receipt tab","Receipt tab ughada"),A.tab("Pro Subscription")],back]);
    else if(key==="report") setChips([[L("Open report tab","Report tab ughada"),A.tab("Comprehensive")],back]);
    else if(key==="forecast") setChips([[L("Open Forecast","Forecast ughada"),A.tab("Forecast")],back]);
    else if(key==="risk") setChips([[L("Open Risk tab","Risk tab ughada"),A.tab("Risk")],back]);
    else if(key==="upload"||key==="missing") setChips([[L("Choose file now","File ata nivada"),A.upload],back]);
    else if(key==="login") setChips([["Login",A.login],back]);
    else if(key==="tabs") setChips([["Sales",A.tab("Sales")],["Cost",A.tab("Cost")],["Profit & Loss",A.tab("Profit")],["Products",A.tab("Products")],["Customers",A.tab("Customers")],back]);
    else mainChips();
  }

  const has=(s,w)=>w.some(x=>s.includes(x));
  function detectLang(raw){
    if(/[\u0900-\u097F]/.test(raw)) return "mr";
    if(/\b(kay|kasa|kashi|kase|kara|karu|aahe|ahe|pahije|nahi|mala|mi|tumhi|sanga|banva|ughada|dya|hoy|ka|madat|kuthe|kon|maza|mazha)\b/i.test(raw)) return "mr";
    if(/\b(how|what|why|where|can|help|pay|download|upload|hello|hi|price|cost|report|receipt|status)\b/i.test(raw)) return "en";
    return lang;
  }
  function handle(raw){
    const s=raw.toLowerCase().trim(); if(!s) return;
    lang=detectLang(raw); S.lang=lang;
    if(has(s,["thank","thanks","dhanyavad","धन्यवाद","आभार"])) return reply("thanks");
    if(has(s,["bye","goodbye"])) return reply("bye");
    if(has(s,["hi","hello","hey","namaste","namaskar","नमस्कार","हॅलो"]) && s.split(/\s+/).length<4) return reply("hello");
    if(has(s,["status","my plan","my subscription","maza","mazha","mazi","active ahe","active aahe","paid ka"])) {add(statusText(),"bot");return mainChips();}
    if(has(s,["required columns","missing","column"])) return reply("missing");
    if(has(s,["utr","reference number","transaction id"])) return reply("utr");
    if(has(s,["receipt","invoice","पावती","pavti"])) return reply("receipt");
    if(has(s,["forecast","predict","future","pudhche","अंदाज"])) return reply("forecast");
    if(has(s,["risk","alert","loss","tota","low margin"])) return reply("risk");
    if(has(s,["filter","date range","no data"])) return reply("filters");
    if(has(s,["theme","dark","light","color","colour","white","purple","green"])) return reply("theme");
    if(has(s,["pay","price","cost of","₹","rupee","upi","subscribe","subscription","plan","paise","पैसे","phonepe","gpay","paytm","card","qr","pdf","download","locked"])) return reply("pay");
    if(has(s,["report","अहवाल"])) return reply("report");
    if(has(s,["upload","file","csv","excel","xlsx","xml","format"])) return reply("upload");
    if(has(s,["tab","tabs","sections","pages"])) return reply("tabs");
    if(has(s,["login","register","sign","account","logout"])) return reply("login");
    if(has(s,["not working","error","problem","issue","fail","chalat nahi","kaam karat nahi","kaam nahi"])) return reply("fail");
    if(has(s,["what is","about","kay aahe","kay ahe","he kay","purpose","features","dashboard"])) return reply("what");
    reply("unknown");
  }

  function setOpen(o){ S.open=o; box.classList.toggle("open",o); if(o){ if(!S.hist.length) reply("hello"); input.focus(); msgs.scrollTop=msgs.scrollHeight; } }
  $("gwAsstBtn").onclick=()=>setOpen(!S.open);
  $("gwAsstX").onclick=()=>setOpen(false);
  function send(){const v=input.value; if(!v.trim())return; add(v,"me"); input.value=""; setTimeout(()=>handle(v),250);}
  $("gwAsstSend").onclick=send;
  input.addEventListener("keydown",e=>{ if(e.key==="Enter"){e.preventDefault();send();} e.stopPropagation(); });

  // restore history after Streamlit re-run
  S.hist.forEach(m=>add(m.t,m.w,false));
  mainChips();
  if(S.open) box.classList.add("open");
 } catch(err){ console.error("[GW assistant] failed:", err); }
})();
</script>
"""


def _assistant_logo_data_uri():
    project_dir = Path(__file__).resolve().parent
    logo_path = project_dir / "clean_high_resolution_modern_ui_style_icon_brand.png"
    if not logo_path.is_file():
        logo_path = project_dir / "logo.png"
    if not logo_path.is_file():
        raise FileNotFoundError("Assistant logo not found: expected the branded icon or logo.png")

    with Image.open(logo_path) as image:
        image = image.convert("RGBA")
        image.thumbnail((128, 128), Image.Resampling.LANCZOS)
        image_bytes = BytesIO()
        image.save(image_bytes, format="PNG", optimize=True)

    encoded = base64.b64encode(image_bytes.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_gw_assistant(T, ctx=None):
    """
    T   : the active theme dict from hh.py  (T = THEMES[theme])
    ctx : optional dict, e.g. dict(dataset=dataset_name, rows=len(filtered_df),
          paid=st.session_state["payment_verified"], user="Name or None")
    """
    ctx = ctx or {}
    cfg = {
        "panel": T.get("panel", "#131f37"),
        "panel2": T.get("panel2", "#1a2b4c"),
        "text": T.get("text", "#f8fafc"),
        "muted": T.get("muted", "#94a3b8"),
        "border": T.get("border", "rgba(255,255,255,0.14)"),
        "accent": T.get("accent", "#38bdf8"),
        "btn_bg": T.get("btn_bg", "linear-gradient(135deg,#0284c7,#6366f1)"),
        "dataset": str(ctx.get("dataset", "-")),
        "rows": int(ctx.get("rows", 0)),
        "paid": bool(ctx.get("paid", False)),
        "user": ctx.get("user") or "",
    }
    payload = json.dumps(cfg).replace("</", "<\\/")
    logo_data_uri = json.dumps(_assistant_logo_data_uri())
    # height=0: the iframe only injects the widget into the main Streamlit page
    st.sidebar.caption("Assistant loaded — look for the  button at the bottom-right.")
    assistant_script = _JS.replace("__CFG__", payload).replace("__LOGO_DATA_URI__", logo_data_uri)
    components.html(assistant_script, height=1)