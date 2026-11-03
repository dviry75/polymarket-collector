/** Native DEMO settings panel. Uses the app's authenticated, CSRF-aware client. */
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses={PENDING:'בתור',SENDING:'נשלח כעת',SENT:'התקבל אצל שרת הדואר',FAILED:'השליחה נכשלה',UNKNOWN:'תוצאה לא ידועה — לא נשלח שוב אוטומטית',CANCELED:'בוטל לאחר שינוי הגדרות',EXPIRED:'פג תוקף ההודעה'};

export function notificationStatus(record){
  const configured=record.transport.state==='CONFIGURED';
  return `<div class="helper" data-mail-status>${configured?'חיבור לספק הוגדר. מסירה לנמען דורשת בדיקת מייל.':'שליחה כבויה בשרת. יש להשלים את החיבור לספק המייל.'}<br>שולח: <b dir="ltr">${escapeHTML(record.sender)}</b><br>בתור: ${Number(record.queue.pending)}${record.queue.dropped?` · הודעות שלא נכנסו לתור עקב עומס: ${Number(record.queue.dropped)}`:''}${record.queue.worker_error?' · מנגנון ההתראות דורש בדיקה':''}</div>
    <h3>מצב שליחות אחרונות</h3>${record.deliveries.length?`<div class="table-wrap"><table><thead><tr><th>נמען</th><th>התראה</th><th>מצב</th><th>ניסיונות</th></tr></thead><tbody>${record.deliveries.map(d=>`<tr><td dir="ltr">${escapeHTML(d.recipient)}</td><td>${escapeHTML(record.catalog.find(e=>e.code===d.event)?.label??(d.event==='TEST'?'בדיקה':d.event))}</td><td>${escapeHTML(statuses[d.status]??d.status)}</td><td>${Number(d.attempts)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">עדיין אין שליחות. קבלת הודעה אצל שרת הדואר אינה הוכחה להגעתה לתיבת הדואר הנכנס.</p>'}`;
}

export function registerNotifications(api){
  customElements.define('demo-notifications',class extends HTMLElement {
    constructor(){
      super();this.record=null;this.saving=false;
      this.addEventListener('submit',event=>{if(event.target.id==='notification-form'){event.preventDefault();event.stopPropagation();this.save();}});
      this.addEventListener('click',event=>{
        const action=event.target.closest('[data-mail-action]')?.dataset.mailAction;
        if(action==='refresh')this.refreshStatus();
        if(action==='test')this.test();
      });
    }
    connectedCallback(){if(!this.initialized){this.initialized=true;this.load();}}
    async load(){
      this.innerHTML='<section class="panel notification-panel"><h2>התראות במייל</h2><p role="status">טוען הגדרות…</p></section>';
      try{const record=await api.notifications();if(!this.isConnected)return;this.record=record;this.render();}
      catch{if(this.isConnected)this.innerHTML='<section class="panel notification-panel"><h2>התראות במייל</h2><p role="alert">לא ניתן לטעון את הגדרות ההתראות. יש לרענן את העמוד.</p></section>';}
    }
    render(){
      const r=this.record,s=r.settings;
      this.innerHTML=`<section class="panel notification-panel"><h2>התראות במייל</h2><p class="muted">נמענים וסוגי התראות לסביבת DEMO. כל הזמנים לפי שעון ישראל.</p>
        <form id="notification-form"><label class="notification-check"><input type="checkbox" name="enabled" ${s.enabled?'checked':''}>הפעלת התראות למייל</label>
        <label class="field">כתובות נמענים<textarea name="recipients" rows="3" dir="ltr" placeholder="name@example.com" maxlength="2600">${escapeHTML(s.recipients.join('\n'))}</textarea><small>עד 10 כתובות; כתובת בכל שורה או הפרדה בפסיקים. כל הודעה נשלחת בנפרד לכל נמען.</small></label>
        <fieldset><legend>סוגי התראות</legend><div class="notification-events">${r.catalog.map(e=>`<label class="notification-check"><input type="checkbox" name="event" value="${escapeHTML(e.code)}" ${s.events.includes(e.code)?'checked':''}>${escapeHTML(e.label)}</label>`).join('')}</div></fieldset>
        <div class="notification-schedule"><label class="field">שעת הסיכום היומי<input name="daily_time" type="time" value="${escapeHTML(s.daily_time)}" required><small>היממה שלפני שעת הסיכום שנבחרה.</small></label>
        <label class="field">תדירות דוח מפורט<select name="periodic_hours">${[1,3,6].map(n=>`<option value="${n}" ${s.periodic_hours===n?'selected':''}>${n===1?'כל שעה':`כל ${n} שעות`}</option>`).join('')}</select><small>בשעות עגולות לפי שעון ישראל; מופעל רק כאשר נבחר דוח תקופתי.</small></label></div>
        <fieldset><legend>ספי חריגה במשאבי השרת</legend><div class="notification-schedule">
        ${[['network_mib_per_second','תעבורה נכנסת + יוצאת (MiB/s)',0.01,100000,0.01],['cpu_percent','CPU בשרת (%)',1,100,1],['ram_percent','RAM בשרת (%)',1,100,1],['disk_percent','ניצול דיסק (%)',1,100,1],['resource_duration_seconds','משך חריגה לפני התראה (שניות)',10,3600,1]].map(([name,label,min,max,step])=>`<label class="field">${label}<input name="${name}" type="number" min="${min}" max="${max}" step="${step}" value="${Number(s[name])}" required></label>`).join('')}
        </div><small>כל מדד מופעל רק אם נבחר בסוגי ההתראות. הודעה אחת לכל חריגה מתמשכת; התראה חדשה תתאפשר לאחר 2 דקות מתחת ל־80% מהסף. הרשת נמדדת לכל השרת וכוללת תוכנות אחרות, ללא loopback.</small></fieldset>
        <p class="muted">דוח מפורט כולל פתיחות, סגירות, רווח/הפסד נקי, עמלות ופירוט עד 30 עסקאות. תקלה נשלחת לאחר 30 שניות, והתאוששות לאחר 10 שניות של תקינות. שינויים חלים על התראות עתידיות.</p>
        <div class="down" role="alert" data-mail-error></div><div role="status" data-mail-feedback></div>
        <div class="notification-actions"><button type="submit" class="primary">שמירת הגדרות התראות</button><button type="button" data-mail-action="test" ${r.transport.state!=='CONFIGURED'||!s.enabled?'disabled':''}>שליחת בדיקה לנמענים השמורים</button><button type="button" data-mail-action="refresh">רענון מצב שליחה</button></div></form>
        <div data-mail-history>${notificationStatus(r)}</div></section>`;
    }
    async save(){
      if(this.saving)return;this.saving=true;
      const form=this.querySelector('form'),fields=new FormData(form);
      const settings={enabled:fields.has('enabled'),recipients:String(fields.get('recipients')).split(/[\s,;]+/).filter(Boolean),events:fields.getAll('event'),daily_time:fields.get('daily_time'),periodic_hours:Number(fields.get('periodic_hours')),...Object.fromEntries(['network_mib_per_second','cpu_percent','ram_percent','disk_percent','resource_duration_seconds'].map(name=>[name,Number(fields.get(name))]))};
      form.querySelector('[type=submit]').disabled=true;this.querySelector('[data-mail-error]').textContent='';
      try{
        const record=await api.saveNotifications(settings,this.record.revision);
        if(!this.isConnected)return;this.record=record;this.render();this.querySelector('[data-mail-feedback]').textContent='הגדרות ההתראות נשמרו.';
      }catch(error){if(this.isConnected)this.querySelector('[data-mail-error]').textContent=error.status===409?'ההגדרות השתנו במקום אחר. יש לרענן את העמוד לפני שמירה.':'לא ניתן לשמור. יש לבדוק כתובות, בחירת התראות ושעת סיכום.';}
      finally{this.saving=false;if(this.isConnected)this.querySelector('[type=submit]').disabled=false;}
    }
    async refreshStatus(){
      try{const record=await api.notifications();if(this.isConnected)this.querySelector('[data-mail-history]').innerHTML=notificationStatus(record);}
      catch{if(this.isConnected)this.querySelector('[data-mail-error]').textContent='לא ניתן לעדכן את מצב השליחה.';}
    }
    async test(){
      if(this.saving||!window.confirm('לשלוח הודעת בדיקה לכל הנמענים השמורים?'))return;
      this.saving=true;const button=this.querySelector('[data-mail-action=test]');button.disabled=true;
      try{await api.testNotifications();if(this.isConnected){this.querySelector('[data-mail-feedback]').textContent='הודעת הבדיקה נוספה לתור. יש לרענן מצב שליחה ולבדוק בתיבת הדואר.';await this.refreshStatus();}}
      catch{if(this.isConnected)this.querySelector('[data-mail-error]').textContent='לא ניתן לשלוח בדיקה. יש לוודא שהשליחה הוגדרה ולהמתין דקה בין בדיקות.';}
      finally{this.saving=false;if(this.isConnected)button.disabled=false;}
    }
  });
}
