// Live quotes straight from the browser (no server delay).
// Primary: TradingView scanner (CORS allowed, includes pre/after-hours). Fallback: CNBC quote service.
(function(){
  const TV={NVDA:"NASDAQ:NVDA",GOOGL:"NASDAQ:GOOGL",TTWO:"NASDAQ:TTWO"};
  function session(){
    const p=Object.fromEntries(new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",hour12:false,weekday:"short",hour:"2-digit",minute:"2-digit"})
      .formatToParts(new Date()).map(x=>[x.type,x.value]));
    if(p.weekday==="Sat"||p.weekday==="Sun")return "closed";
    const m=(+p.hour%24)*60+(+p.minute);
    return m>=240&&m<570?"pre":m>=570&&m<960?"reg":m>=960&&m<1200?"post":"closed";
  }
  const num=x=>{if(x==null)return null;const v=parseFloat(String(x).replace(/[^0-9.\-]/g,""));return isFinite(v)?v:null};
  function pick(s,close,chg,pre,preChg,post,postChg){
    if(s==="pre"&&pre)return {price:pre,chg:preChg,ses:"pre",close};
    if((s==="post"||s==="closed")&&post)return {price:post,chg:postChg,ses:s==="post"?"post":"closed",close};
    return {price:close,chg:chg,ses:s,close};
  }
  async function tv(syms){
    const r=await fetch("https://scanner.tradingview.com/america/scan",{method:"POST",headers:{"Content-Type":"text/plain"},
      body:JSON.stringify({symbols:{tickers:syms.map(s=>TV[s]||"NASDAQ:"+s)},
        columns:["close","change","premarket_close","premarket_change","postmarket_close","postmarket_change","high","low","volume"]})});
    if(!r.ok)throw new Error("tv "+r.status);
    const j=await r.json(),s=session(),out={};
    for(const row of j.data||[]){
      const sym=row.s.split(":")[1],d=row.d;
      // after 20:00 ET and before 04:00 the scanner's post-market fields still hold the evening session
      out[sym]=Object.assign(pick(s,d[0],d[1],d[2],d[3],s==="pre"?null:d[4],d[5]),{high:d[6],low:d[7],volume:d[8],src:"TradingView"});
    }
    return out;
  }
  async function cnbc(syms){
    const r=await fetch("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="+syms.join("%7C")+
      "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json");
    if(!r.ok)throw new Error("cnbc "+r.status);
    const j=await r.json(),s=session(),out={};
    let q=j.FormattedQuoteResult&&j.FormattedQuoteResult.FormattedQuote;q=Array.isArray(q)?q:[q];
    for(const x of q){if(!x)continue;const e=x.ExtendedMktQuote||{};
      out[x.symbol]=Object.assign(pick(s,num(x.last),num(x.change_pct),num(e.last),num(e.change_pct),num(e.last),num(e.change_pct)),{src:"CNBC"});}
    return out;
  }
  async function get(syms){
    let res=null;
    try{res=await tv(syms)}catch(e){}
    if(!res||!Object.keys(res).length){try{res=await cnbc(syms)}catch(e){}}
    if(res)for(const k in res)res[k].t=Date.now();
    return res||{};
  }
  // poll(syms, cb, ms): calls cb(quotes) now and every ms while the tab is visible
  function poll(syms,cb,ms){
    const run=()=>{if(document.hidden)return;get(syms).then(q=>{if(Object.keys(q).length)cb(q)})};
    run();setInterval(run,ms||15000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)run()});
  }
  const SES={pre:"🌅 Pre-market",reg:"☀️ ตลาดเปิด",post:"🌙 After-hours",closed:"😴 ตลาดปิด"};
  window.LiveQuote={get,poll,session,SES};
})();
