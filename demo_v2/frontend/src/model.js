/** Pure presentation/domain mappings. Financial values stay decimal strings. */
export const assets = ['BTC','ETH','SOL','XRP','DOGE','HYPE','BNB'];
export const frames = ['5m','15m','1h'];
/** @type {Record<string,number>} */
export const durations = {'5m':300,'15m':900,'1h':3600};
/** @type {Record<string,string>} */
export const names = {BTC:'Bitcoin',ETH:'Ethereum',SOL:'Solana',XRP:'XRP',DOGE:'Dogecoin',HYPE:'Hyperliquid',BNB:'BNB'};
export const days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
/** @param {unknown} value */
function decimalParts(value) {
  if (typeof value !== 'string' || !/^[+-]?\d+(\.\d+)?$/.test(value)) throw new Error('Invalid decimal string');
  const [whole, fraction = ''] = value.replace(/^[+-]/, '').split('.');
  return { units: BigInt(whole + fraction) * (value.startsWith('-') ? -1n : 1n), scale: fraction.length };
}
/** Exact comparison, including values too large or precise for Number. @param {string} a @param {string} b */
export function compareDecimal(a,b) {
  const x = decimalParts(a), y = decimalParts(b), scale = Math.max(x.scale,y.scale);
  const left=x.units*10n**BigInt(scale-x.scale), right=y.units*10n**BigInt(scale-y.scale);
  return left < right ? -1 : left > right ? 1 : 0;
}
/** Display only; half-up using integer arithmetic. Raw API string is preserved separately. @param {string|null|undefined} value @param {number} [digits] @param {boolean} [signed] */
export function formatDecimal(value, digits=2, signed=false) {
  if (value == null) return '—';
  try {
    const {units,scale} = decimalParts(value), negative=units<0n, absolute=negative?-units:units;
    const factor=10n**BigInt(Math.max(0,scale-digits));
    const rounded=scale>digits?(absolute+factor/2n)/factor:absolute*10n**BigInt(digits-scale);
    const padded=rounded.toString().padStart(digits+1,'0');
    return (negative&&rounded?'−':signed&&rounded?'+':'')+(digits?padded.slice(0,-digits)+'.'+padded.slice(-digits):padded);
  } catch { return '—'; }
}
/** @param {string|null|undefined} value @param {boolean} [signed] */
export const formatMoney = (value,signed=false) => {const display=formatDecimal(value,2,signed);return display==='—'?display:display.replace(/^(−|\+)?/, '$1$');};
export const freshRule = () => ({id:'',versionId:'',name:'',markets:['BTC 5m'],entry:'0.74',shares:'5',frequency:1,tpOn:false,tp:'0.96',slOn:false,sl:'0.66',enabled:true,archived:false,schedule:/** @type {{day:number,start:string,end:string}[]} */([]),windows:/** @type {{start:number,end:number}[]} */([]),conditions:/** @type {{op:string,measure:string,direction:string,min:string,max:string}[]} */([]),tradeCount:0,wallet:'0',pnl:'0'});
/** @param {any} row */
export function mapRule(row) {
  const s=row.spec;
  /** @type {Record<string,string>} */const comparisons={GREATER_THAN:'Greater Than',LESS_THAN:'Less Than',BETWEEN:'Between'};
  /** @type {Record<string,string>} */const directions={POSITIVE:'Positive',NEGATIVE:'Negative',ABSOLUTE:'Either / Absolute'};
  return {...freshRule(),id:row.id,versionId:row.current_version_id,name:s.name,markets:s.markets.map((/** @type {string} */m)=>m.replace('-',' ')),entry:s.entry_trigger,shares:s.gross_shares,frequency:s.entry_frequency,
    tpOn:s.take_profit!=null,tp:s.take_profit??'0.96',slOn:s.stop_loss!=null,sl:s.stop_loss??'0.66',enabled:s.enabled,archived:s.archived,
    schedule:s.weekly_schedule.map((/** @type {any} */v)=>({day:(v.weekday+1)%7,start:v.start,end:v.end})),
    windows:s.entry_windows.map((/** @type {any} */v)=>({start:v.start_seconds,end:v.end_seconds})),
    conditions:s.underlying_filters.map((/** @type {any} */v)=>({measure:v.measurement==='PERCENT'?'Percentage':'Price Amount',op:comparisons[v.comparison],direction:directions[v.direction],min:v.lower,max:v.upper??''})),
    tradeCount:row.trade_count,wallet:row.wallet,pnl:row.net_pnl??null};
}
/** @param {ReturnType<typeof freshRule>} r */
export function rulePayload(r) {
  return {name:r.name.trim(),markets:r.markets.map(m=>m.replace(' ','-')),entry_trigger:r.entry,gross_shares:r.shares,entry_frequency:r.frequency,
    take_profit:r.tpOn?r.tp:null,stop_loss:r.slOn?r.sl:null,enabled:r.enabled,archived:r.archived,
    weekly_schedule:r.schedule.map(v=>({weekday:(v.day+6)%7,start:v.start,end:v.end})),entry_windows:r.windows.map(v=>({start_seconds:v.start,end_seconds:v.end})),
    underlying_filters:r.conditions.map(v=>({measurement:v.measure==='Percentage'?'PERCENT':'ABSOLUTE',comparison:({'Greater Than':'GREATER_THAN','Less Than':'LESS_THAN','Between':'BETWEEN'})[v.op],direction:({'Positive':'POSITIVE','Negative':'NEGATIVE','Either / Absolute':'ABSOLUTE'})[v.direction],lower:v.min,upper:v.op==='Between'?v.max:null}))};
}
/** @param {ReturnType<typeof freshRule>} r */
export function validateRule(r) {
  const errors=[];
  const valid=(/** @type {string} */v)=>{try{decimalParts(v);return true;}catch{return false;}};
  if(!r.name.trim()||r.name.trim().length>120)errors.push('יש להזין שם כלל באורך עד 120 תווים.');
  if(!r.markets.length||new Set(r.markets).size!==r.markets.length||r.markets.some(m=>!assets.flatMap(a=>frames.map(tf=>a+' '+tf)).includes(m)))errors.push('יש לבחור שווקים נתמכים.');
  if(!valid(r.entry)||compareDecimal(r.entry,'0')<=0||compareDecimal(r.entry,'1')>=0)errors.push('מחיר הכניסה חייב להיות גדול מ־0 וקטן מ־1.');
  if(!valid(r.shares)||compareDecimal(r.shares,'5')<0)errors.push('גודל העסקה המינימלי הוא 5 מניות.');
  if(!Number.isSafeInteger(r.frequency)||r.frequency<1)errors.push('תדירות הכניסה חייבת להיות מספר שלם חיובי.');
  if(r.tpOn&&(!valid(r.tp)||!valid(r.entry)||compareDecimal(r.tp,r.entry)<=0||compareDecimal(r.tp,'1')>0))errors.push('יעד הרווח חייב להיות מעל מחיר הכניסה ועד 1.');
  if(r.slOn&&(!valid(r.sl)||!valid(r.entry)||compareDecimal(r.sl,r.entry)>=0||compareDecimal(r.sl,'0')<0))errors.push('עצירת ההפסד חייבת להיות מ־0 ועד מתחת למחיר הכניסה.');
  if(r.schedule.some(s=>!Number.isInteger(s.day)||s.day<0||s.day>6||!/^\d{2}:\d{2}(:\d{2})?$/.test(s.start)||!/^\d{2}:\d{2}(:\d{2})?$/.test(s.end)||s.start===s.end))errors.push('לכל טווח שבועי נדרשות שעות התחלה וסיום שונות.');
  if(r.windows.some(w=>!Number.isSafeInteger(w.start)||!Number.isSafeInteger(w.end)||w.start<0||w.end<=w.start))errors.push('לכל חלון נדרשים זמני התחלה וסיום תקינים בשניות מתחילת השוק.');
  for(const c of r.conditions)if(!valid(c.min)||compareDecimal(c.min,'0')<0||c.op==='Between'&&(!valid(c.max)||compareDecimal(c.max,c.min)<0))errors.push('גבולות התנועה חייבים להיות לא שליליים; הגבול העליון חייב להיות לפחות הגבול התחתון.');
  return [...new Set(errors)];
}
/** First instant on a Jerusalem calendar date, including DST, independent of browser timezone.
 * Binary search avoids assuming a fixed offset or a 24-hour day. @param {string} day */
export function jerusalemDayStart(day) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(day))throw new Error('Invalid date');
  const center=Date.parse(day+'T00:00:00Z');
  if(!Number.isFinite(center)||new Date(center).toISOString().slice(0,10)!==day)throw new Error('Invalid date');
  const formatter=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Jerusalem',year:'numeric',month:'2-digit',day:'2-digit'});
  let lo=center-86400000,hi=center+86400000;
  while(lo<hi){const mid=Math.floor((lo+hi)/2);if(formatter.format(new Date(mid))<day)lo=mid+1;else hi=mid;}
  return new Date(lo).toISOString();
}
/** @param {Record<string,string>} f @returns {Record<string,string>} */
export function cohortQuery(f) {
  /** @type {Record<string,string>} */const query={};
  for(const [ui,api] of Object.entries({rule:'rule_id',asset:'asset',tf:'timeframe',side:'side',status:'status',min:'movement_min',max:'movement_max'}))if(f[ui]!==undefined&&f[ui]!=='')query[api]=f[ui];
  if(f.market){const [asset,timeframe]=f.market.split(' ');query.asset=asset;query.timeframe=timeframe;}
  if(f.from)query.from_date=jerusalemDayStart(f.from);
  if(f.to){const next=new Date(f.to+'T12:00:00Z');next.setUTCDate(next.getUTCDate()+1);query.to_date=jerusalemDayStart(next.toISOString().slice(0,10));}
  query.movement_measurement=f.measure==='amount'?'ABSOLUTE':'PERCENT';
  return query;
}
/** @param {any} row @param {ReturnType<typeof mapRule>[]} [rules] */
export function mapTrade(row,rules=[]) {
  const [asset,tf]=(row.market?.definition_id??[row.asset,row.timeframe].join('-')).split('-');
  const entry=row.entry_provenance?.underlying, exit=row.exit_provenance?.underlying;
  return {raw:row,id:row.id,ruleId:row.rule_id,ruleName:row.rule_snapshot?.name??rules.find(r=>r.id===row.rule_id)?.name??row.rule_id,asset,tf,market:asset+' '+tf,side:row.side,status:row.status,
    entry:row.entry_price,exit:row.exit_price,shares:row.gross_shares,fees:row.total_fees,pnl:row.net_pnl,entered:row.entry_at,exited:row.exit_at,reason:row.exit_reason,
    underlyingOpen:entry?.baseline?.price??null,underlyingEntry:entry?.current?.price??null,underlyingExit:exit?.current?.price??null,movementEntry:row.underlying_movements?.open_to_entry??null,movementExit:row.underlying_movements?.open_to_exit??null};
}
/** Backend freshness is necessary; local elapsed time may only make data stale, never ready. @param {any} snapshot @param {number} [now] */
export function marketState(snapshot,now=Date.now()) {
  if(!snapshot)return 'UNAVAILABLE';
  if(!snapshot.listening)return 'NOT_LISTENING';
  const observed=Date.parse(snapshot.observed_at);
  return snapshot.fresh&&Number.isFinite(observed)&&now-observed<=5000&&now>=observed-1000?'FRESH':'STALE';
}
/** @param {any} health */
export const healthState=health=>['READY','DEGRADED','NOT_READY'].includes(health?.state)?health.state:'UNKNOWN';
