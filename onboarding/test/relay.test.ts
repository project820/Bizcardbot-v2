import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import worker, {PairingsObject, type Env, type Transport} from '../src/index';

// SQLite-backed DO's KV interface, transactions and input gate are modeled locally.
class MemoryStorage {
  data = new Map<string, any>();
  alarm: number | null = null;
  async get<T>(key: string): Promise<T | undefined> { return structuredClone(this.data.get(key)); }
  async put(key: string, value: unknown) { this.data.set(key, structuredClone(value)); }
  async delete(key: string | string[]) { for (const k of typeof key === 'string' ? [key] : key) this.data.delete(k); }
  async list(options: {prefix?: string} = {}) { return new Map([...this.data].filter(([key]) => key.startsWith(options.prefix ?? '')).map(([k,v]) => [k, structuredClone(v)])); }
  async setAlarm(value: number) { this.alarm = value; }
  async getAlarm() { return this.alarm; }
  async deleteAlarm() { this.alarm = null; }
  async transaction<T>(fn: (tx: DurableObjectTransaction) => Promise<T>): Promise<T> {
    const before = structuredClone(this.data);
    try { return await fn(this as unknown as DurableObjectTransaction); }
    catch (error) { this.data = before; throw error; }
  }
}
class MemoryState {
  gate: Promise<unknown> = Promise.resolve();
  constructor(readonly storage: MemoryStorage) {}
  blockConcurrencyWhile<T>(fn: () => Promise<T>): Promise<T> {
    const task = this.gate.then(fn); this.gate = task.catch(() => {}); return task;
  }
}
let env: Env, storage: MemoryStorage, object: PairingsObject;
let transport: ReturnType<typeof vi.fn<Transport>>;
let calls: {method: string; body: any}[];
let now = 1800000000000;
const secret = 'fixture-webhook-secret';
const botToken = '123456:fixture-managed-token';
const bot = {id: 123456, is_bot: true, first_name: '명함봇', username: 'my_card_bot'};
const user = {id: 456, is_bot: false, first_name: '사용자'};
function req(path: string, method = 'GET', body?: unknown, headers: Record<string,string> = {}) {
  return new Request(`https://relay.example${path}`, {method, headers: {'content-type':'application/json',...headers}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
}
async function create(ip = '192.0.2.1', name = '내 명함봇') {
  const response = await worker.fetch(req('/v1/pairings', 'POST', {bot_name: name}, {'CF-Connecting-IP': ip}), env);
  expect(response.status).toBe(201); return await response.json() as any;
}
function pairId(pair: any): string {return new URL(pair.deep_link).searchParams.get('start')!;}
function action(pair: any, path = 'status', body: unknown = {}, token = pair.poll_token) {
  return worker.fetch(req(`/v1/pairings/${path}`, 'POST', body, {Authorization: `Bearer ${token}`}), env);
}
function poll(pair: any, token = pair.poll_token) {return action(pair,'status',{},token);}
function webhook(update: unknown, key = secret, ip = '149.154.160.1') {
  return worker.fetch(req('/telegram/webhook', 'POST', update, {'X-Telegram-Bot-Api-Secret-Token': key,'CF-Connecting-IP':ip}), env);
}
function start(pair: any, who = user, chatType = 'private') {
  return webhook({message: {from: who, chat: {id: who.id, type: chatType}, text: `/start ${pairId(pair)}`}});
}
function managed(who = user, managedBot = bot) {return webhook({managed_bot: {user: who, bot: managedBot}});}
function created(who = user, managedBot = bot, chatType = 'private') {
  return webhook({message:{from:who,chat:{id:who.id,type:chatType},managed_bot_created:{bot:managedBot}}});
}
function code(): string {
  const message = [...calls].reverse().find(c=>c.method==='sendMessage' && /설치 화면에 이 코드를 입력하세요: \d{6}/.test(c.body.text));
  expect(message).toBeDefined(); return message!.body.text.match(/입력하세요: (\d{6})/)[1];
}
function claim(pair: any, value = code()) {return action(pair,'claim',{code:value});}
async function ready() {const p = await create(); await start(p); await created(); return p;}
function snapshot() {return JSON.stringify([...storage.data]);}
beforeEach(() => {
  now = 1800000000000; vi.spyOn(Date, 'now').mockImplementation(() => now);
  calls = []; storage = new MemoryStorage();
  transport = vi.fn(async (input, init) => {
    const method = String(input).split('/').at(-1)!;
    calls.push({method, body: JSON.parse(String(init?.body))});
    return Response.json({ok: true, result: method === 'getManagedBotToken' ? botToken : true});
  });
  env = {MANAGER_BOT_TOKEN: 'fixture-manager-token', MANAGER_WEBHOOK_SECRET: secret, MANAGER_BOT_USERNAME: 'BizcardSetupBot',
    PAIRINGS: {idFromName: () => 'singleton', get: () => ({fetch: (request: Request) => object.fetch(request)})} as unknown as DurableObjectNamespace};
  object = new PairingsObject(new MemoryState(storage) as unknown as DurableObjectState, env, transport);
  vi.stubGlobal('fetch', vi.fn(() => {throw new Error('External calls forbidden');}));
});
afterEach(() => {vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();});

describe('pairing contract v2', () => {
  it('creates opaque poll credentials and only exposes pairing id in deep_link', async () => {
    const p=await create();
    expect(Object.keys(p).sort()).toEqual(['deep_link','expires_at','poll_token','suggested_username']);
    expect(pairId(p)).toMatch(/^[A-Za-z0-9_-]{22,64}$/);
    expect(p.poll_token).toMatch(/^[A-Za-z0-9_-]{43,}$/);
    expect(p.suggested_username).toMatch(/^bizcard_[a-z0-9]{6}_bot$/);
    expect(p.expires_at).toBe(now/1000+600);
    expect(new URL(p.deep_link).pathname).toBe('/BizcardSetupBot');
    expect(snapshot()).not.toContain(p.poll_token);
    expect(await (await poll(p)).json()).toEqual({status:'pending'});
  });
  it('offers a persistent creation keyboard and a separate encoded fallback link', async () => {
    const name='명함 & 이름'; const p=await create('192.0.2.1',name); await start(p);
    expect(await (await poll(p)).json()).toEqual({status:'started'});
    const keyboard=calls[0].body.reply_markup;
    expect(keyboard.resize_keyboard).toBe(true); expect(keyboard).not.toHaveProperty('one_time_keyboard');
    const button=keyboard.keyboard[0][0];
    expect(button).toMatchObject({text:'내 명함봇 만들기',request_managed_bot:{suggested_name:name,suggested_username:p.suggested_username}});
    const id=button.request_managed_bot.request_id;
    expect(Number.isInteger(id) && id >= -2147483648 && id <= 2147483647).toBe(true);
    expect(calls[1].body.reply_markup.inline_keyboard[0][0]).toEqual({text:'버튼이 안 보이면 여기를 누르세요',url:`https://t.me/newbot/BizcardSetupBot/${p.suggested_username}?name=${encodeURIComponent(name)}`});
  });
  it('does not hand an attacker with poll_token a victim token without the Telegram code', async () => {
    const p=await ready();
    expect(await (await poll(p)).json()).toEqual({status:'created'});
    const response=await action(p,'claim',{});
    expect(response.status).toBe(403); expect(await response.json()).toEqual({status:'bad_code',attempts_left:4});
    expect(calls.filter(c=>c.method==='getManagedBotToken')).toHaveLength(0);
    expect(snapshot()).not.toContain(code()); expect(snapshot()).not.toContain(botToken);
    expect(calls.filter(c=>c.method==='sendMessage').at(-1)?.body.chat_id).toBe(user.id);
  });
  it('fetches the token only at claim and allows retry until ack', async () => {
    const p=await ready(); const secretCode=code();
    const replies=await Promise.all([claim(p,secretCode),claim(p,secretCode)]);
    for(const response of replies) {
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({status:'ready',bot_token:botToken,bot_id:bot.id,bot_username:bot.username,owner_user_id:user.id,access_restricted:true});
    }
    expect(calls.filter(c=>c.method==='getManagedBotToken').map(c=>c.body)).toEqual([{user_id:bot.id},{user_id:bot.id}]);
    expect(calls.find(c=>c.method==='setManagedBotAccessSettings')?.body).toEqual({user_id:bot.id,is_access_restricted:true});
    expect(await (await poll(p)).json()).toEqual({status:'created'});
    expect(snapshot()).not.toContain(botToken); expect(snapshot()).not.toContain(secretCode);
    const response=await action(p,'ack'); expect(response.status).toBe(200); expect(await response.json()).toEqual({status:'claimed'});
    expect(calls.at(-1)?.body).toEqual({chat_id:user.id,text:`완료: @${bot.username} 에서 명함 사진을 보내세요`,reply_markup:{remove_keyboard:true}});
    for(const path of ['status','claim','ack']) {const r=await action(p,path,{code:secretCode}); expect(r.status).toBe(410); expect(await r.json()).toEqual({status:'claimed'});}
    const entries=[...storage.data].filter(([k])=>!k.startsWith('rate:'));
    expect(entries).toHaveLength(1);
    expect(Object.keys(entries[0][1]).sort()).toEqual(['expiresAt','purgeAt','status']);
    expect(snapshot()).not.toContain(bot.username); expect(snapshot()).not.toContain(pairId(p));
  });
  it('expires after five incorrect codes even with parallel attempts', async () => {
    const p=await ready(); const wrong=code()==='000000'?'000001':'000000';
    const responses=await Promise.all(Array.from({length:5},()=>claim(p,wrong)));
    for(let i=0;i<4;i++) {expect(responses[i].status).toBe(403); expect(await responses[i].json()).toEqual({status:'bad_code',attempts_left:4-i});}
    expect(responses[4].status).toBe(410); expect(await responses[4].json()).toEqual({status:'expired'});
    expect((await claim(p)).status).toBe(410); expect((await poll(p)).status).toBe(410);
    expect(calls.filter(c=>c.method==='getManagedBotToken')).toHaveLength(0);
  });
  it('rejects claim and ack before the required states', async () => {
    const p=await create(); let r=await claim(p,'123456'); expect(r.status).toBe(409); expect(await r.json()).toEqual({status:'pending'});
    await start(p); r=await claim(p,'123456'); expect(r.status).toBe(409); expect(await r.json()).toEqual({status:'started'});
    await created(); expect((await action(p,'ack')).status).toBe(409);
  });
  it('ignores token and owner changes after creation and ignores duplicate messages', async () => {
    const p=await ready(); const secretCode=code(); const count=calls.length;
    await managed(user,{...bot,id:789,username:'replacement_bot'}); await created();
    expect(calls).toHaveLength(count);
    expect(await (await claim(p,secretCode)).json()).toMatchObject({bot_id:bot.id,bot_username:bot.username});
  });
  it.each([false,true])('uses managed_bot only for a matching owner and suggested username (uppercase=%s)', async (uppercase) => {
    await managed(); const p=await create(); await managed();
    const matchingBot={...bot,username:uppercase?p.suggested_username.toUpperCase():p.suggested_username};
    expect(await (await poll(p)).json()).toEqual({status:'pending'});
    await start(p); await managed({...user,id:999},matchingBot);
    expect(await (await poll(p)).json()).toEqual({status:'started'});
    await managed(user,matchingBot); expect(await (await poll(p)).json()).toEqual({status:'created'});
    expect(await (await claim(p)).json()).toMatchObject({bot_id:bot.id,bot_username:matchingBot.username});
  });
  it('ignores an existing bot change arriving before the new bot creation message', async () => {
    const p=await create(); await start(p); const count=calls.length;
    await managed(user,{...bot,id:888888,username:'preexisting_bot'});
    expect(await (await poll(p)).json()).toEqual({status:'started'});
    expect(calls).toHaveLength(count);
    expect((await claim(p,'123456')).status).toBe(409);
    expect(calls.filter(c=>c.method==='getManagedBotToken')).toHaveLength(0);
    // The primary creation message still accepts a user-edited handle.
    expect(bot.username).not.toBe(p.suggested_username);
    await created();
    expect(await (await claim(p)).json()).toMatchObject({bot_id:bot.id,bot_username:bot.username});
    expect(calls.filter(c=>c.method==='getManagedBotToken').map(c=>c.body)).toEqual([{user_id:bot.id}]);
  });
  it('refuses another user on an already bound link with an explicit message', async () => {
    const p=await create(); await start(p); await start(p,{...user,id:999});
    expect(calls.at(-1)?.body).toMatchObject({chat_id:999,text:expect.stringContaining('이미 사용된 링크')});
    await created({...user,id:999}); expect(await (await poll(p)).json()).toEqual({status:'started'});
    await created(); expect(await (await claim(p)).json()).toMatchObject({owner_user_id:user.id});
  });
  it('does not bind group chats, bots, or mismatched message senders', async () => {
    const p=await create(); await start(p,user,'group'); await start(p,{...user,is_bot:true});
    expect(await (await poll(p)).json()).toEqual({status:'pending'});
    await start(p); await created(user,bot,'group');
    await webhook({message:{from:user,chat:{id:999,type:'private'},managed_bot_created:{bot}}});
    expect(await (await poll(p)).json()).toEqual({status:'started'});
  });
  it('invalidates the previous pairing when the same owner starts another', async () => {
    const p=await ready(); const q=await create(); await start(q);
    expect((await poll(p)).status).toBe(410); expect((await claim(p)).status).toBe(410);
    await created(user,{...bot,id:789}); expect(await (await claim(q)).json()).toMatchObject({bot_id:789});
  });
  it('expires at exactly 600 seconds, including after a claim, and purges tombstones', async () => {
    const p=await ready(); await claim(p); now+=600000; await object.alarm();
    const r=await poll(p); expect(r.status).toBe(410); expect(await r.json()).toEqual({status:'expired'});
    expect((await claim(p)).status).toBe(410); expect(snapshot()).not.toContain(botToken);
    now+=86400001; await object.alarm(); expect(storage.data.size).toBe(0);
  });
  it('returns 404 for unknown and malformed bearer tokens without exposing ids in paths', async () => {
    const p=await ready();
    for(const token of ['wrong','a'.repeat(43),'']) expect((await poll(p,token)).status).toBe(404);
    expect((await worker.fetch(req(`/v1/pairings/${pairId(p)}`),env)).status).toBe(404);
    await claim(p); await action(p,'ack'); expect((await poll(p,'a'.repeat(43))).status).toBe(404);
  });
  it('limits creation to ten per IP per ten minutes through durable counters', async () => {
    for(let i=0;i<10;i++) await create();
    object=new PairingsObject(new MemoryState(storage) as unknown as DurableObjectState,env,transport);
    expect((await worker.fetch(req('/v1/pairings','POST',{bot_name:'명함봇'},{'CF-Connecting-IP':'192.0.2.1'}),env)).status).toBe(429);
    await create('192.0.2.2'); now+=600000; await create(); expect(snapshot()).not.toContain('192.0.2.1');
  });
  it('checks secret and both Telegram CIDRs by default, with an explicit IP opt-out', async () => {
    expect((await webhook({},'wrong')).status).toBe(401);
    for(const ip of ['149.154.160.0','149.154.175.255','91.108.4.0','91.108.7.255']) expect((await webhook({},secret,ip)).status).toBe(200);
    for(const ip of ['149.154.159.255','149.154.176.0','91.108.3.255','91.108.8.0','192.0.2.1','','149.154.160.999','::ffff:149.154.160.1']) expect((await webhook({},secret,ip)).status).toBe(403);
    env.TELEGRAM_IP_CHECK='0'; expect((await webhook({},secret,'192.0.2.1')).status).toBe(200);
    expect((await webhook({},'wrong','192.0.2.1')).status).toBe(401);
    env.TELEGRAM_IP_CHECK='1'; expect((await webhook({},secret,'192.0.2.1')).status).toBe(403);
  });
  it('rejects invalid names, types, JSON, large bodies and unknown routes', async () => {
    for(const name of ['', ' ', 'a'.repeat(65),null,123,'bad\nname']) expect((await worker.fetch(req('/v1/pairings','POST',{bot_name:name}),env)).status).toBe(400);
    expect((await worker.fetch(new Request('https://relay.example/v1/pairings',{method:'POST',headers:{'content-type':'application/json'},body:'{'}),env)).status).toBe(400);
    expect((await worker.fetch(req('/v1/pairings','POST',{bot_name:'a'},{'content-type':'text/plain'}),env)).status).toBe(415);
    expect((await worker.fetch(req('/v1/pairings','POST',{bot_name:'a'.repeat(20000)}),env)).status).toBe(413);
    expect((await webhook({padding:'a'.repeat(70000)})).status).toBe(413);
    expect((await worker.fetch(req('/'),env)).status).toBe(200);
    expect((await worker.fetch(req('/telegram/webhook'),env)).status).toBe(404);
  });
  it('does not let a stalled POST occupy the DO gate, then times out that body', async () => {
    const p=await create(); vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});
    let canceled=false;
    const body=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(new TextEncoder().encode('{'));},cancel(){canceled=true;}});
    const slow=worker.fetch(new Request('https://relay.example/v1/pairings',{method:'POST',headers:{'content-type':'application/json'},body,duplex:'half'} as RequestInit),env);
    expect(await (await poll(p)).json()).toEqual({status:'pending'});
    expect((await start(p)).status).toBe(200);
    await vi.advanceTimersByTimeAsync(5001); expect((await slow).status).toBe(408); expect(canceled).toBe(true);
    vi.useRealTimers();
  });
  it('rejects an oversized chunked body before calling the DO', async () => {
    const get=vi.spyOn(env.PAIRINGS,'get');
    const body=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(new Uint8Array(1025));controller.close();}});
    const r=await worker.fetch(new Request('https://relay.example/v1/pairings',{method:'POST',headers:{'content-type':'application/json'},body,duplex:'half'} as RequestInit),env);
    expect(r.status).toBe(413); expect(get).not.toHaveBeenCalled();
  });
  it('keeps created after transient token lookup failure and retries claim', async () => {
    const p=await ready(); transport.mockRejectedValueOnce(new Error('fixture-error'));
    expect((await claim(p)).status).toBe(503); expect(await (await poll(p)).json()).toEqual({status:'created'});
    expect((await claim(p)).status).toBe(200);
  });
  it('discloses access restriction failure without storing tokens', async () => {
    const original=transport.getMockImplementation()!;
    transport.mockImplementation((input,init)=>String(input).endsWith('/setManagedBotAccessSettings') ? Promise.resolve(Response.json({ok:false},{status:400})) : original(input,init));
    const p=await ready(); expect(await (await claim(p)).json()).toMatchObject({access_restricted:false}); expect(snapshot()).not.toContain(botToken);
  });
  it('does not deliver tokens if the pairing expires during the API call', async () => {
    const p=await ready(); const original=transport.getMockImplementation()!;
    transport.mockImplementation(async (input,init)=>{const r=await original(input,init); if(String(input).endsWith('/getManagedBotToken'))now+=600000; return r;});
    expect((await claim(p)).status).toBe(410); expect(snapshot()).not.toContain(botToken);
  });
  it('preserves code verification and claims across object reconstruction', async () => {
    const p=await ready(); const value=code();
    object=new PairingsObject(new MemoryState(storage) as unknown as DurableObjectState,env,transport);
    expect((await claim(p,value)).status).toBe(200); await action(p,'ack');
    object=new PairingsObject(new MemoryState(storage) as unknown as DurableObjectState,env,transport);
    expect((await poll(p)).status).toBe(410);
  });
  it('does not log request secrets or raw API failures', async () => {
    const spies=['log','info','warn','error','debug'].map(method=>vi.spyOn(console,method as 'log').mockImplementation(()=>{}));
    const p=await ready(); transport.mockRejectedValueOnce(new Error(`${env.MANAGER_BOT_TOKEN} ${p.poll_token} ${pairId(p)} ${botToken}`)); await claim(p);
    const logs=JSON.stringify(spies.flatMap(spy=>spy.mock.calls));
    for(const value of [env.MANAGER_BOT_TOKEN,secret,botToken,p.poll_token,pairId(p),code()]) expect(logs).not.toContain(value);
  });
});
