/** Read-only resource presentation. Host network counters are never DEMO traffic. */
export function formatBytes(value, rate=false){
  if(typeof value!=='number'||!Number.isFinite(value)||value<0)return '—';
  const units=['B','KiB','MiB','GiB','TiB'];
  let index=0;
  while(value>=1024&&index<units.length-1){value/=1024;index++;}
  return `${value.toLocaleString('en-US',{maximumFractionDigits:index?1:0})} ${units[index]}${rate?'/s':''}`;
}
const percent=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?`${value.toFixed(1)}%`:'—';
const meter=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?`<div class="resource-meter" aria-hidden="true"><span style="width:${Math.min(100,value)}%"></span></div>`:'';
export function resourceState(resources,now=Date.now(),failed=false){
  if(failed||!resources?.sampled_at||!['AVAILABLE','PARTIAL'].includes(resources.state))return 'UNAVAILABLE';
  const age=now-Date.parse(resources.sampled_at);
  return !Number.isFinite(age)||age<0||age>15000?'STALE':resources.state;
}
export function resourcesHTML(resources,now=Date.now(),failed=false){
  const state=resourceState(resources,now,failed),available=state==='AVAILABLE'||state==='PARTIAL';
  const r=available?resources:{},p=r?.process??{},h=r?.host??{},d=r?.disk??{},n=r?.network??{};
  const scope=p.scope==='DEMO_SERVICE_PROCESSES'?'תהליכי DEMO':'תהליך DEMO';
  const label=({AVAILABLE:'מתעדכן',PARTIAL:'מדידה חלקית',STALE:'נתונים לא עדכניים',UNAVAILABLE:'המדידה אינה זמינה'})[state];
  const sampled=available?new Intl.DateTimeFormat('he-IL',{timeZone:'Asia/Jerusalem',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(resources.sampled_at)):'—';
  return `<section class="resource-panel" data-testid="resource-panel" data-resource-state="${state}" aria-label="משאבי מערכת"><div class="resource-heading"><div><h3>משאבי מערכת</h3><p>דגימה קלה ברקע · תצוגה בלבד</p></div><span class="resource-status">${label}<small>${sampled}</small></span></div><div class="resource-grid">
    <article class="resource-card" data-resource="cpu"><div class="resource-label"><span>CPU</span><small>${scope}</small></div><strong class="resource-value">${percent(p.cpu_percent)}</strong><p>100% = ליבה אחת</p>${meter(h.cpu_percent)}<div class="resource-caption">עומס השרת כולו <b dir="ltr">${percent(h.cpu_percent)}</b></div></article>
    <article class="resource-card" data-resource="ram"><div class="resource-label"><span>RAM</span><small>${scope}</small></div><strong class="resource-value">${formatBytes(p.rss_bytes)}</strong><p>סכום זיכרון תהליכי הדמו; כולל דפים משותפים</p>${meter(h.ram_percent)}<div class="resource-caption">RAM בשרת <b dir="ltr">${formatBytes(h.ram_used_bytes)} / ${formatBytes(h.ram_total_bytes)}</b></div></article>
    <article class="resource-card" data-resource="disk"><div class="resource-label"><span>אחסון</span><small>נתוני DEMO</small></div><strong class="resource-value">${formatBytes(r?.demo_storage?.total_bytes)}</strong><p>מסד נתונים וקובצי WAL / SHM</p>${meter(d.percent)}<div class="resource-caption">פנוי בדיסק <b dir="ltr">${formatBytes(d.available_bytes)}</b></div><small class="resource-total">נפח הדיסק ${formatBytes(d.total_bytes)}</small></article>
    <article class="resource-card" data-resource="network"><div class="resource-label"><span>תעבורת רשת</span><small>השרת כולו</small></div><div class="resource-network"><div><small>קבלה ↓</small><strong class="resource-value">${formatBytes(n.received_bytes_per_second,true)}</strong></div><div><small>שליחה ↑</small><strong class="resource-value">${formatBytes(n.sent_bytes_per_second,true)}</strong></div></div><div class="resource-caption">מצטבר: קבלה <b dir="ltr">${formatBytes(n.received_bytes)}</b> · שליחה <b dir="ltr">${formatBytes(n.sent_bytes)}</b></div></article>
  </div><p class="resource-note">תעבורת השרת כוללת תוכנות אחרות ורשת מקומית; סכום הממשקים ללא loopback, וייתכנו ממשקים וירטואליים. זו אינה מדידה נפרדת של הדמו או של החיבור הביתי לאינטרנט.${available&&(p.cpu_percent==null||n.received_bytes_per_second==null)?' קצב התעבורה וה־CPU זמינים לאחר שתי דגימות.':''}</p></section>`;
}
