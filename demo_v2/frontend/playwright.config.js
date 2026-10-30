import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./tests/e2e',testMatch:'*.spec.js',workers:1,fullyParallel:false,timeout:60000,
  use:{baseURL:'http://127.0.0.1:52972/demo/',headless:true,viewport:{width:1440,height:1000},timezoneId:'America/New_York',
    launchOptions:{executablePath:process.env.DEMO_TEST_CHROME||'/usr/bin/google-chrome',args:['--no-sandbox','--disable-dev-shm-usage']},trace:'retain-on-failure'},
  webServer:[
    {command:(process.env.DEMO_TEST_PYTHON||'python3')+' tests/e2e/backend_fixture.py',env:{PYTHONPATH:'../..:../../polymarket-collector',PYTHONDONTWRITEBYTECODE:'1'},url:'http://127.0.0.1:52971/health',reuseExistingServer:false,timeout:20000,gracefulShutdown:{signal:'SIGTERM',timeout:5000}},
    {command:'node scripts/serve.mjs --built',env:{DEMO_FRONTEND_PORT:'52972',DEMO_BACKEND_TARGET:'http://127.0.0.1:52971'},url:'http://127.0.0.1:52972/demo/',reuseExistingServer:false,timeout:20000,gracefulShutdown:{signal:'SIGTERM',timeout:5000}}
  ]
});
