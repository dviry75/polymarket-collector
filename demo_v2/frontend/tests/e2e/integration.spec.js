import {test,expect} from '@playwright/test';
const prefix='/demo/api/v1';
async function login(page){
  await page.goto('./');await expect(page.locator('#login-form')).toBeVisible();
  await page.locator('[name=username]').fill('frontend-e2e');await page.locator('[name=password]').fill('frontend-test-only');
  await page.locator('#login-form button[type=submit]').click();await expect(page.locator('.app')).toBeVisible();
  await expect(page.locator('.loading')).toHaveCount(0);
}
async function api(page,path){const response=await page.request.get(prefix+path);expect(response.ok()).toBeTruthy();return response.json();}
async function confirm(page){await expect(page.locator('[role=dialog]')).toBeVisible();await page.locator('[data-action=confirm]').click();await expect(page.locator('[role=dialog]')).toHaveCount(0);}

async function checkNotificationSettings(page){
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.locator('[data-nav=maintenance]').click();
  const form=page.locator('#notification-form');await expect(form).toBeVisible();
  await form.locator('[name=recipients]').fill('first@example.com\nsecond@example.com');
  await form.locator('[name=enabled]').check();
  await form.locator('[name=event][value=NETWORK_HIGH]').check();
  await form.locator('[name=event][value=RAM_HIGH]').check();
  await form.locator('[name=network_mib_per_second]').fill('2.5');
  await form.locator('[name=ram_percent]').fill('85');
  await form.locator('[name=resource_duration_seconds]').fill('90');
  for(const hours of ['1','3','6']){
    await form.locator('[name=periodic_hours]').selectOption(hours);
    await form.locator('[name=daily_time]').fill('09:30');
    await form.locator('[name=event][value=POSITION_OPENED]').uncheck();
    await form.locator('[type=submit]').click();
    await expect(page.locator('[data-mail-feedback]')).toContainText('נשמרו');
    const saved=await api(page,'/notifications');
    expect(saved.settings.periodic_hours).toBe(Number(hours));
    expect(saved.settings.recipients).toEqual(['first@example.com','second@example.com']);
    expect(saved.settings.events).toContain('PERIODIC_SUMMARY');
    expect(saved.settings.events).toContain('NETWORK_HIGH');
    expect(saved.settings.events).toContain('RAM_HIGH');
    expect(saved.settings.events).not.toContain('CPU_HIGH');
    expect(saved.settings.network_mib_per_second).toBe(2.5);
    expect(saved.settings.ram_percent).toBe(85);
    expect(saved.settings.resource_duration_seconds).toBe(90);
    expect(saved.settings.events).not.toContain('POSITION_OPENED');
  }
  const first=page.locator('[data-recipient-email="first@example.com"]');
  const second=page.locator('[data-recipient-email="second@example.com"]');
  for(const section of [first,second])for(const input of await section.locator('input').all())await input.uncheck();
  await first.locator('[value=SYSTEM_FAULT]').check();
  await first.locator('[value=NETWORK_HIGH]').check();
  await second.locator('[value=DAILY_SUMMARY]').check();
  await second.locator('[value=PERIODIC_SUMMARY]').check();
  await form.locator('[type=submit]').click();
  await expect(page.locator('[data-mail-feedback]')).toContainText('נשמרו');
  const subscriptions=(await api(page,'/notifications')).settings.recipient_events;
  expect(subscriptions['first@example.com']).toEqual(['SYSTEM_FAULT','NETWORK_HIGH']);
  expect(subscriptions['second@example.com']).toEqual(['DAILY_SUMMARY','PERIODIC_SUMMARY']);
  await form.locator('[name=recipients]').fill('first@example.com\nsecond@example.com\nthird@example.com');
  await form.locator('[data-mail-action=recipients]').click();
  await expect(first.locator('[value=NETWORK_HIGH]')).toBeChecked();
  const third=page.locator('[data-recipient-email="third@example.com"]');
  await expect(third.locator('[value=NETWORK_HIGH]')).toBeChecked();
  for(const input of await third.locator('input').all())await input.uncheck();
  await form.locator('[type=submit]').click();
  await expect(page.locator('[data-mail-feedback]')).toContainText('נשמרו');
  expect((await api(page,'/notifications')).settings.recipient_events['third@example.com']).toEqual([]);
  await form.locator('[name=recipients]').fill('first@example.com\nsecond@example.com');
  await form.locator('[type=submit]').click();
  await expect(page.locator('[data-mail-feedback]')).toContainText('נשמרו');
  expect((await api(page,'/notifications')).settings.recipient_events).not.toHaveProperty('third@example.com');
  await form.locator('[name=recipients]').fill('unsaved@example.com');
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await expect(form.locator('[name=recipients]')).toHaveValue('unsaved@example.com');
  await page.reload();await expect(page.locator('#notification-form')).toBeVisible();
  await expect(page.locator('[name=recipients]')).toHaveValue('first@example.com\nsecond@example.com');
  await expect(page.locator('[name=network_mib_per_second]')).toHaveValue('2.5');
  await expect(first.locator('[value=NETWORK_HIGH]')).toBeChecked();
  await expect(first.locator('[value=DAILY_SUMMARY]')).not.toBeChecked();
  await expect(second.locator('[value=DAILY_SUMMARY]')).toBeChecked();
  await expect(second.locator('[value=NETWORK_HIGH]')).not.toBeChecked();
  await page.screenshot({path:'test-results/per-recipient-notifications-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  expect(await page.locator('.notification-panel').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/per-recipient-notifications-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await expect(page.locator('[name=event][value=NETWORK_HIGH]')).toBeChecked();
  await expect(page.locator('[data-mail-action=test]')).toBeDisabled();
  await expect(page.locator('[data-mail-history]')).toContainText('שליחה כבויה בשרת');
  const privateFields=await api(page,'/notifications');
  expect(JSON.stringify(privateFields)).not.toContain('SMTP_PASSWORD');expect(privateFields.deliveries).toEqual([]);
  await page.locator('[name=recipients]').fill('');await page.locator('[name=enabled]').uncheck();
  await page.locator('#notification-form [type=submit]').click();
  await expect(page.locator('[data-mail-feedback]')).toContainText('נשמרו');
  await page.setViewportSize({width:390,height:844});
  expect(await page.locator('.notification-panel').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/notification-settings-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:'test-results/notification-settings-desktop.png',fullPage:true});
  expect(errors).toEqual([]);
}

test('real cookie login, server cohorts/pagination/detail, complete rule actions, confirmed resets and logout',async({page})=>{
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await login(page);
  await checkNotificationSettings(page);
  await page.locator('[data-nav=trades]').last().click();
  const kpi=await api(page,'/kpi');
  await expect(page.locator('[data-kpi="Total trades"]')).toHaveText(String(kpi.total_trades));
  await expect(page.locator('[data-kpi="Wallet"]')).toHaveText('−$16.48');
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(10);
  const tradePage=await api(page,'/trades?limit=10&offset=0');
  expect(await page.locator('[data-trade-serial]').allTextContents()).toEqual(tradePage.items.map(t=>'#'+t.serial_number));
  await page.locator('[data-page="2"]').first().click();await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(2);
  const expectedSerials=(await api(page,'/trades?limit=100')).items.map(t=>'#'+t.serial_number);
  for(const size of ['25','50','100']){
    const fetched=page.waitForResponse(response=>{const url=new URL(response.url());return url.pathname===prefix+'/trades'&&url.searchParams.get('limit')===size&&url.searchParams.get('offset')==='0';});
    await page.locator('[data-page-size]').selectOption(size);await fetched;
    await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(12);
    expect(await page.locator('[data-trade-serial]').allTextContents()).toEqual(expectedSerials);
    await expect(page.locator('.table-foot')).toContainText('1–12');
    await expect(page.getByRole('button',{name:'לעמוד הבא',exact:true})).toBeDisabled();
  }
  await page.locator('[data-filter=status]').selectOption('OPEN');
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(4);
  await expect(page.locator('[data-page-size]')).toHaveValue('100');
  await page.reload();await expect(page.locator('[data-page-size]')).toHaveValue('100');
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(12);
  await page.setViewportSize({width:390,height:844});
  expect(await page.locator('.table-controls').evaluate(el=>el.getBoundingClientRect().right<=window.innerWidth)).toBeTruthy();
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-page-size]').selectOption('10');
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(10);

  await page.locator('[data-page="1"]').first().click();await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(10);
  await page.locator('[data-trade]').first().click();
  await expect(page.locator('#dialog-title')).toHaveText('עסקה #'+tradePage.items[0].serial_number);
  await expect(page.locator('[role=dialog]')).toContainText(tradePage.items[0].id);
  await expect(page.locator('[role=dialog]')).toContainText('מניות נטו');
  await expect(page.locator('[role=dialog]')).toContainText('עמלת קנייה');await page.locator('[data-action=close]').click();
  await page.locator('[data-filter=status]').selectOption('OPEN');await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(4);
  await expect(page.locator('[data-kpi="Total trades"]')).toHaveText('4');
  await page.locator('[data-action=clear]').click();await expect(page.locator('[data-kpi="Total trades"]')).toHaveText('12');
  await page.locator('[data-frame="1h"]').click();await expect(page.locator('.market').filter({hasText:'ETH'})).toContainText('0.7300');
  await page.locator('[data-action=health]').first().click();await expect(page.locator('[role=dialog]')).not.toContainText('CURRENT_FEE_ROUNDING_UNVERIFIED');await page.locator('[data-action=close]').click();
  await expect(page.locator('.banner')).not.toContainText('חישוב העמלות');
  await page.screenshot({path:'test-results/trade-serials-desktop.png',fullPage:true});
  expect(await page.locator('.market').evaluateAll(cards=>cards.every(card=>card.scrollWidth<=card.clientWidth))).toBeTruthy();
  await page.screenshot({path:'test-results/overview-desktop.png',fullPage:true});

  await page.locator('[data-action=create]').click();await expect(page.locator('#rule-form')).toBeVisible();
  await page.locator('[data-draft=name]').fill('כלל דפדפן <safe>');
  await page.locator('[data-market="HYPE 1h"]').check();
  await page.locator('[data-draft=entry]').fill('0.740000000000000001');await page.locator('[data-draft=shares]').fill('5.0000000000000001');
  await page.locator('[data-draft=frequency]').fill('2');
  await page.locator('[data-draft-toggle=tpOn]').click();await page.locator('[data-draft=tp]').fill('1');
  await page.locator('[data-draft-toggle=slOn]').click();await page.locator('[data-draft=sl]').fill('0');
  await page.locator('[data-day="0"]').check();await page.locator('[data-schedule="0"][data-key=start]').fill('22:00:15');await page.locator('[data-schedule="0"][data-key=end]').fill('02:00:30');
  await page.locator('[data-add-range="0"]').click();
  await page.locator('[data-add-window]').click();await page.locator('[data-window][data-key=start]').fill('01:00');await page.locator('[data-window][data-key=end]').fill('02:00');await page.locator('[data-window][data-key=end]').blur();
  await page.locator('[data-action=add-condition]').click();await page.locator('[data-condition="0"][data-key=op]').selectOption('Between');await page.locator('[data-condition="0"][data-key=direction]').selectOption('Negative');
  await page.locator('[data-condition="0"][data-key=min]').fill('0.2');await page.locator('[data-condition="0"][data-key=max]').fill('0.4');
  await page.locator('#rule-form button[type=submit]').click();await expect(page.locator('[data-testid="rules-table"]')).toBeVisible();
  let rule=(await api(page,'/rules')).items.find(r=>r.spec.name==='כלל דפדפן <safe>');expect(rule).toBeTruthy();
  expect(rule.spec.markets).toEqual(['BTC-5m','HYPE-1h']);expect(rule.spec.entry_trigger).toBe('0.740000000000000001');expect(rule.spec.gross_shares).toBe('5.0000000000000001');expect(rule.spec.weekly_schedule[0]).toEqual({weekday:6,start:'22:00:15',end:'02:00:30'});expect(rule.spec.weekly_schedule).toHaveLength(2);expect(rule.spec.entry_windows).toEqual([{start_seconds:60,end_seconds:120}]);expect(rule.spec.underlying_filters[0].direction).toBe('NEGATIVE');
  await expect(page.locator('table')).toContainText('כלל דפדפן <safe>');
  const id=rule.id;
  await page.locator(`[data-edit="${id}"]`).first().click();await page.locator('[data-draft=name]').fill('כלל דפדפן מעודכן');await page.locator('#rule-form button[type=submit]').click();
  await expect(page.locator('[data-testid="rules-table"]')).toContainText('כלל דפדפן מעודכן');
  await page.locator(`[data-toggle-rule="${id}"]`).click();await expect(page.locator(`[data-toggle-rule="${id}"]`)).toHaveAttribute('aria-checked','false');
  await page.locator(`[data-toggle-rule="${id}"]`).click();await expect(page.locator(`[data-toggle-rule="${id}"]`)).toHaveAttribute('aria-checked','true');
  const before=(await api(page,'/rules')).items.length;
  await page.locator(`[data-duplicate="${id}"]`).click();await expect(page.locator('[data-testid="rules-table"] tbody tr')).toHaveCount(before+1);
  const duplicate=(await api(page,'/rules')).items.find(r=>r.spec.name==='כלל דפדפן מעודכן (copy)');expect(duplicate.spec.enabled).toBe(false);
  await page.locator(`[data-delete="${id}"]`).click();await confirm(page);await expect(page.locator(`[data-delete="${id}"]`)).toHaveCount(0);
  expect((await api(page,'/rules/'+id)).spec.archived).toBe(true);expect((await api(page,'/trades')).total).toBe(12);
  await page.locator('[data-nav=maintenance]').click();
  await page.locator('[data-action=reset-trades]').click();await page.locator('[data-action=close]').click();expect((await api(page,'/trades')).total).toBe(12);
  await page.locator('[data-action=reset-trades]').click();await confirm(page);expect((await api(page,'/trades')).total).toBe(0);expect((await api(page,'/rules')).items).toHaveLength(before+1);
  await page.locator('[data-action=factory-reset]').click();await expect(page.locator('[data-action=confirm]')).toBeDisabled();await page.locator('#confirm-text').fill('wrong');await expect(page.locator('[data-action=confirm]')).toBeDisabled();await page.locator('#confirm-text').fill('RESET DEMO');await confirm(page);expect((await api(page,'/rules')).items).toHaveLength(0);
  await page.locator('[data-action=logout]').click();await expect(page.locator('#login-form')).toBeVisible();
  expect((await page.request.get(prefix+'/auth/current')).status()).toBe(401);expect(errors).toEqual([]);
});

test('expired session, API outage with no sample fallback, loading, empty and mobile layout',async({page})=>{
  await login(page);
  const session=await api(page,'/auth/current');
  expect((await page.request.post(prefix+'/maintenance/factory-reset',{headers:{'X-Demo-CSRF':session.csrf_token},data:{confirmation:'FACTORY_RESET_DEMO'}})).ok()).toBeTruthy();
  await page.locator('[data-action=refresh]').first().click();await expect(page.locator('[data-kpi="Total trades"]')).toHaveText('0');
  await page.route('**/demo/api/v1/**',route=>route.abort('failed'));
  await page.locator('[data-action=refresh]').first().click();await expect(page.locator('[role=alert]')).toContainText('לא ניתן לעדכן');await expect(page.locator('[data-kpi="Wallet"]')).toHaveText('—');await expect(page.locator('[data-testid="trades-table"]')).toHaveCount(0);
  await page.unroute('**/demo/api/v1/**');
  await page.locator('[data-action=refresh]').first().click();await expect(page.locator('[data-kpi="Wallet"]')).toHaveText('$0.00');
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'test-results/overview-mobile.png',fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  expect(await page.locator('.market').evaluateAll(cards=>cards.every(card=>card.scrollWidth<=card.clientWidth))).toBeTruthy();
  await page.context().clearCookies();await page.locator('[data-action=refresh]').first().click();await expect(page.locator('#login-form')).toBeVisible();await expect(page.locator('#login-form')).toContainText('הסשן פג');
  await page.locator('[name=username]').fill('frontend-e2e');await page.locator('[name=password]').fill('wrong');await page.locator('#login-form button[type=submit]').click();await expect(page.locator('[role=alert]')).toContainText('שם המשתמש או הסיסמה שגויים');
  expect(await page.evaluate(()=>[localStorage.length,sessionStorage.length])).toEqual([0,0]);
});

test('concurrent rule edits retain draft on 409, original names remain literal, dirty logout is confirmed',async({page})=>{
  const errors=[];page.on('pageerror',error=>errors.push(error.message));await login(page);
  const session=await api(page,'/auth/current');
  const created=await page.request.post(prefix+'/rules',{headers:{'X-Demo-CSRF':session.csrf_token},data:{name:'BTC Momentum',markets:['BTC-5m'],entry_trigger:'0.74',gross_shares:'5'}});
  expect(created.status()).toBe(201);const rule=await created.json();
  await page.locator('[data-action=refresh]').first().click();
  await expect(page.locator('[data-filter=rule]')).toContainText('BTC Momentum');
  await page.goto('./#rule/edit/'+rule.id);await expect(page.locator('[data-draft=name]')).toHaveValue('BTC Momentum');
  await page.locator('[data-draft=name]').fill('שינוי שלא נשמר');
  const changed=await page.request.put(prefix+'/rules/'+rule.id,{headers:{'X-Demo-CSRF':session.csrf_token},data:{spec:{...rule.spec,name:'Changed elsewhere'},expected_version_id:rule.current_version_id}});expect(changed.ok()).toBeTruthy();
  await page.locator('#rule-form button[type=submit]').click();await expect(page.locator('#form-errors')).toContainText('הנתונים השתנו');
  await expect(page.locator('[data-draft=name]')).toHaveValue('שינוי שלא נשמר');
  const refreshed=page.waitForResponse(response=>response.url().endsWith(prefix+'/health'));
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await refreshed;
  await expect(page.locator('#form-errors')).toContainText('הנתונים השתנו');await expect(page.locator('[data-draft=name]')).toHaveValue('שינוי שלא נשמר');
  await page.locator('[data-action=logout]').click();await confirm(page);await expect(page.locator('#login-form')).toBeVisible();expect(errors).toEqual([]);
});


test('fee-enabled market: UI rule, fresh non-trigger then exact trigger creates a flat-fee simulated position',async({page})=>{
  await login(page);
  const session=await api(page,'/auth/current');
  const headers={'X-Demo-CSRF':session.csrf_token};
  await page.request.post(prefix+'/maintenance/factory-reset',{headers,data:{confirmation:'FACTORY_RESET_DEMO'}});
  await page.locator('[data-action=create]').click();
  await page.locator('[data-draft=name]').fill('Flat fee exact entry');
  await page.locator('[data-market="BTC 5m"]').uncheck();
  await page.locator('[data-market="ETH 1h"]').check();
  await page.locator('[data-draft=entry]').fill('0.74');
  await page.locator('[data-draft=shares]').fill('5');
  await page.locator('#rule-form button[type=submit]').click();
  await expect(page.locator('[data-testid="rules-table"]')).toContainText('Flat fee exact entry');
  const rule=(await api(page,'/rules')).items.find(r=>r.spec.name==='Flat fee exact entry');
  const before=await page.request.post(prefix+'/__test__/observation',{headers,data:{ask:'0.75'}});
  expect(before.ok()).toBeTruthy();const observed=await before.json();expect(observed.processed).toBe(2);expect(observed.fees_enabled).toBe(true);
  expect((await api(page,'/trades')).total).toBe(0);
  const triggered=await page.request.post(prefix+'/__test__/observation',{headers,data:{ask:'0.74'}});
  expect(triggered.ok()).toBeTruthy();
  expect((await triggered.json()).exact_fee_provider_configured).toBe(false);
  const trades=await api(page,'/trades');expect(trades.total).toBe(1);
  const row=trades.items[0];
  expect(row.rule_id).toBe(rule.id);expect(row.status).toBe('OPEN');
  expect(row.entry_price).toBe('0.74');expect(row.buy_fee).toBe('0.068');
  expect(row.cash_debit).toBe('3.768');expect(row.net_shares).toBe('5');
  expect(row.fee_evidence.method).toBe('EMPIRICAL_FLAT_AVERAGE');
  expect(row.entry_provenance.observation.source).toBe('BROWSER_FAKE_FEE_ENABLED_FEED');
  expect((await api(page,'/health')).blocker_codes).not.toContain('CURRENT_FEE_ROUNDING_UNVERIFIED');
  await page.locator('[data-nav=trades]').last().click();
  await page.locator('[data-action=refresh]').first().click();
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(1);
  await expect(page.locator('[data-testid="trades-table"]')).toContainText('Flat fee exact entry');
  await expect(page.locator('[data-testid="trades-table"]')).toContainText('$0.068');
  await page.screenshot({path:'test-results/flat-fee-entry.png',fullPage:true});
  await page.locator('[data-nav=maintenance]').click();
  await expect(page.locator('.maintenance')).toContainText('$0.068');
  await page.screenshot({path:'test-results/flat-fee-methodology.png',fullPage:true});
  await checkHealthResources(page);
});


async function checkHealthResources(page){
  await page.locator('[data-nav=trades]').last().click();
  await expect.poll(async()=>((await api(page,'/health')).resources.process.cpu_percent),{timeout:10000}).not.toBeNull();
  await page.locator('[data-action=refresh]').first().click();
  await page.locator('[data-action=health]').first().click();
  const panel=page.locator('[data-testid=resource-panel]');
  await expect(panel).toHaveAttribute('data-resource-state','AVAILABLE');
  await expect(panel.locator('[data-resource]')).toHaveCount(4);
  for(const text of ['CPU','RAM','אחסון','תעבורת רשת','השרת כולו','תהליך DEMO'])await expect(panel).toContainText(text);
  await expect(panel.locator('[data-resource=ram] .resource-value')).not.toHaveText('—');
  await expect(panel.locator('[data-resource=network]')).toContainText('/s');
  const first=await panel.locator('.resource-status small').textContent();
  // Existing 10-second API poll updates the open drawer; no close/reopen needed.
  await expect(panel.locator('.resource-status small')).not.toHaveText(first,{timeout:15000});
  await page.evaluate(()=>document.fonts.ready);
  expect(await page.evaluate(()=>document.fonts.check('14px Rubik','אבג'))).toBe(true);
  expect(await page.locator('body').evaluate(el=>getComputedStyle(el).fontFamily)).toContain('Rubik');
  await page.screenshot({path:'test-results/health-resources-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  expect(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBeTruthy();
  expect(await page.locator('[role=dialog]').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/health-resources-mobile.png',fullPage:true});
  // Failed/stale health must never keep looking live in the open panel.
  await page.route('**/demo/api/v1/health',route=>route.abort('failed'));
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await expect(panel).toHaveAttribute('data-resource-state','UNAVAILABLE');
  await expect(panel.locator('[data-resource=ram] .resource-value')).toHaveText('—');
}
