import { applyD1Migrations, env } from 'cloudflare:test';

await env.DB.exec('PRAGMA foreign_keys = ON;');
await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
