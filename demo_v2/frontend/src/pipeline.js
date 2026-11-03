/** Cached pipeline counters; unknown values stay unknown. No decision inputs. */
const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
const count=value=>number(value)?.toLocaleString('he-IL')??'—';
const ms=value=>number(value)==null?'—':`${(value*1000).toFixed(1)} ms`;
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function pipelineHTML(pipeline,failed=false){
  if(!pipeline||failed)return '';
  const streams=Object.entries(pipeline.streams??{});
  return `<section class="resource-panel" aria-label="צינור נתוני השוק"><div class="resource-heading"><div><h3>קליטה ומנוע החלטות</h3><p>תורים מסודרים · שמירה כספית אטומית · חסימה לפי השוק שנפגע</p></div><span class="resource-status">${pipeline.owner_alive?'תהליך החלטות פעיל':'תהליך החלטות אינו זמין'}</span></div><div class="resource-grid"><article class="resource-card"><div class="resource-label">עיכוב לולאת הקליטה</div><strong class="resource-value" dir="ltr">${ms(pipeline.loop_lag_seconds)}</strong><p>שיא מאז ההפעלה: ${ms(pipeline.loop_lag_max_seconds)}</p></article><article class="resource-card"><div class="resource-label">תגובה מתהליך ההחלטות</div><strong class="resource-value" dir="ltr">${ms(pipeline.owner_last_rpc_seconds)}</strong><p>זמן הבקשה האחרונה</p></article>${streams.map(([key,s])=>`<article class="resource-card"><div class="resource-label">תור ${escape(key)}</div><strong class="resource-value" dir="ltr">${count(s.depth)} / ${count(s.capacity)}</strong><p>התצפית הוותיקה: ${ms(s.oldest_seconds)}</p><div class="resource-caption">נקלטו ${count(s.published)} · נמסרו ${count(s.delivered)}</div><small>${escape(s.reason||'מסודר ותקין')}</small></article>`).join('')}</div><p class="resource-note">מצב DEGRADED מאפשר כניסות רק בשווקים ובכללים שכל התלויות שלהם תקינות. תקלה במסד או במנוע חוסמת את כולם. מדדי התצוגה אינם מסננים תצפיות למסחר.</p></section>`;
}
