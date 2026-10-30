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

test('real cookie login, server cohorts/pagination/detail, complete rule actions, confirmed resets and logout',async({page})=>{
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await login(page);
  const kpi=await api(page,'/kpi');
  await expect(page.locator('[data-kpi="Total trades"]')).toHaveText(String(kpi.total_trades));
  await expect(page.locator('[data-kpi="Wallet"]')).toHaveText('−$16.48');
  await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(10);
  await page.locator('[data-page="2"]').first().click();await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(2);
  await page.locator('[data-page="1"]').first().click();await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(10);
  await page.locator('[data-trade]').first().click();await expect(page.locator('[role=dialog]')).toContainText('מניות נטו');
  await expect(page.locator('[role=dialog]')).toContainText('עמלת קנייה');await page.locator('[data-action=close]').click();
  await page.locator('[data-filter=status]').selectOption('OPEN');await expect(page.locator('[data-testid="trades-table"] tbody tr')).toHaveCount(4);
  await expect(page.locator('[data-kpi="Total trades"]')).toHaveText('4');
  await page.locator('[data-action=clear]').click();await expect(page.locator('[data-kpi="Total trades"]')).toHaveText('12');
  await page.locator('[data-frame="1h"]').click();await expect(page.locator('.market').filter({hasText:'ETH'})).toContainText('0.7300');
  await page.locator('[data-action=health]').first().click();await expect(page.locator('[role=dialog]')).toContainText('CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED');await page.locator('[data-action=close]').click();
  await expect(page.locator('.banner')).toContainText('חישוב העמלות');
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
