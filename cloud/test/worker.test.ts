import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import worker, { JobObject, buildPerson, estimateCostKrw, selectPhoto, type Env, type Job } from '../src/index';
import photo from './fixtures/photo-update.json';
import save from './fixtures/save-update.json';
import card from './fixtures/card.json';
import queued from './fixtures/queued-job.json';

// Model durable storage transactions and the object's input gate; no external APIs are called.
class MemoryStorage {
  data = new Map<string, unknown>();
  puts: Job[] = [];
  alarm: number | null = null;
  async get<T>(key: string): Promise<T | undefined> { return structuredClone(this.data.get(key)) as T | undefined; }
  async put(key: string, value: unknown) {
    this.data.set(key, structuredClone(value));
    if (key === 'job') this.puts.push(structuredClone(value) as Job);
  }
  async setAlarm(value: number) { this.alarm = value; }
  async deleteAlarm() { this.alarm = null; }
  async deleteAll() { this.data.clear(); }
  async transaction<T>(operation: (tx: DurableObjectTransaction) => Promise<T>): Promise<T> {
    const data = new Map(this.data), alarm = this.alarm, length = this.puts.length;
    try { return await operation(this as unknown as DurableObjectTransaction); }
    catch (error) { this.data = data; this.alarm = alarm; this.puts.length = length; throw error; }
  }
}
class MemoryState {
  gate: Promise<unknown> = Promise.resolve();
  constructor(readonly storage: MemoryStorage) {}
  blockConcurrencyWhile<T>(operation: () => Promise<T>): Promise<T> {
    const task = this.gate.then(operation);
    this.gate = task.catch(() => {});
    return task;
  }
  waitUntil(task: Promise<unknown>) { tasks.push(task); }
}
class MemoryNamespace {
  objects = new Map<string, {storage: MemoryStorage; object: JobObject}>();
  idFromName(name: string) { return name; }
  entry(name = '900001') {
    if (!this.objects.has(name)) {
      const storage = new MemoryStorage();
      this.objects.set(name, {storage, object: new JobObject(new MemoryState(storage) as unknown as DurableObjectState, env)});
    }
    return this.objects.get(name)!;
  }
  get(id: string) {
    const entry = this.entry(id);
    return {fetch: (request: Request | string) => entry.object.fetch(typeof request === 'string' ? new Request(request) : request)};
  }
  async job(id = '900001') { return this.entry(id).storage.get<Job>('job'); }
  restart(id = '900001') {
    const entry = this.entry(id);
    entry.object = new JobObject(new MemoryState(entry.storage) as unknown as DurableObjectState, env);
    return entry.object;
  }
}
let jobs: MemoryNamespace, env: Env, tasks: Promise<unknown>[], requests: {url: string; body: any; init?: RequestInit}[];
type Outbound = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
let fetchMock: ReturnType<typeof vi.fn<Outbound>>;
const ctx = {waitUntil(task: Promise<unknown>) { tasks.push(task); }} as ExecutionContext;
const req = (body: unknown, secret = 'fixture-secret') => new Request('http://localhost/webhook', {
  method: 'POST', headers: {'content-type': 'application/json', 'X-Telegram-Bot-Api-Secret-Token': secret}, body: JSON.stringify(body),
});
async function drain() { while (tasks.length) await Promise.all(tasks.splice(0)); }
async function draft() { expect((await worker.fetch(req(photo), env, ctx)).status).toBe(200); await drain(); }
async function confirm() { expect((await worker.fetch(req(save), env, ctx)).status).toBe(200); await drain(); }
function seed(status: Job['status'], extra: Partial<Job> = {}) {
  const now = Date.now();
  const job: Job = {id: '900001', updateId: 900001, chatId: 424242, fileId: 'fixture-medium',
    status, card, createdAt: now, expiresAt: now + 604800000, ...extra};
  jobs.entry().storage.data.set('job', structuredClone(job));
  return job;
}
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return {promise, resolve};
}
beforeEach(() => {
  jobs = new MemoryNamespace(); tasks = []; requests = [];
  env = {JOBS: jobs as unknown as DurableObjectNamespace, TELEGRAM_BOT_TOKEN: 'fixture-only',
    TELEGRAM_WEBHOOK_SECRET: 'fixture-secret', ALLOWED_CHAT_IDS: '424242', OPENAI_API_KEY: 'fixture-only',
    GOOGLE_CLIENT_ID: 'fixture-only', GOOGLE_CLIENT_SECRET: 'fixture-only', GOOGLE_REFRESH_TOKEN: 'fixture-only', DRY_RUN_SAVE: '0'};
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const body = init?.headers && (init.headers as Record<string, string>)['content-type'] === 'application/x-www-form-urlencoded'
      ? Object.fromEntries(new URLSearchParams(String(init.body))) : init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({url, body, init});
    if (url.includes('/getFile')) return Response.json({ok: true, result: {file_path: 'photos/fixture.jpg'}});
    if (url.includes('/file/')) return new Response(new Uint8Array([255, 216, 255, 217]), {headers: {'content-type': 'image/jpeg'}});
    if (url.includes('api.openai.com')) return Response.json({status: 'completed', usage: {input_tokens: 1000, output_tokens: 300},
      output: [{content: [{type: 'output_text', text: JSON.stringify(card)}]}]});
    if (url.includes('oauth2.googleapis.com')) return Response.json({access_token: 'fixture-access-token'});
    if (url.includes('people.googleapis.com')) return Response.json({resourceName: 'people/fixture'});
    if (url.includes('api.telegram.org/bot')) return Response.json({ok: true, result: {message_id: 11}});
    throw new Error('Unexpected outbound request');
  });
  vi.stubGlobal('fetch', fetchMock); vi.spyOn(console, 'log').mockImplementation(() => {});
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('webhook admission', () => {
  it('rejects missing and invalid secrets without storage or outbound calls', async () => {
    expect((await worker.fetch(req(photo, ''), env, ctx)).status).toBe(401);
    expect((await worker.fetch(req(photo, 'wrong'), env, ctx)).status).toBe(401);
    expect(jobs.objects.size).toBe(0); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('rejects unauthorized and group chats', async () => {
    const denied = structuredClone(photo); denied.message.chat.id = 99;
    expect((await worker.fetch(req(denied), env, ctx)).status).toBe(403);
    const group = structuredClone(photo); group.message.chat.type = 'group';
    expect((await worker.fetch(req(group), env, ctx)).status).toBe(403); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('persists queued and update receipt before processing and deduplicates update_id', async () => {
    expect((await worker.fetch(req(photo), env, ctx)).status).toBe(200);
    expect(jobs.entry().storage.puts[0]).toMatchObject({status: 'queued', updateId: 900001});
    expect(await jobs.entry().storage.get('update:900001')).toBe(true);
    expect((await worker.fetch(req(photo), env, ctx)).status).toBe(200); await drain();
    expect(requests.filter(x => x.url.includes('api.openai.com'))).toHaveLength(1);
  });
  it('acknowledges before a slow upstream draft completes', async () => {
    const held = deferred(), started = deferred(), previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      if (String(url).includes('api.openai.com')) { started.resolve(); await held.promise; }
      return previous(url, init);
    });
    expect((await worker.fetch(req(photo), env, ctx)).status).toBe(200);
    await started.promise; expect(await jobs.job()).toMatchObject({status: 'drafting'});
    held.resolve(); await drain();
  });
  it('returns retryable failure when admission cannot persist', async () => {
    vi.spyOn(jobs.entry().storage, 'put').mockRejectedValueOnce(new Error('unavailable'));
    expect((await worker.fetch(req(photo), env, ctx)).status).toBe(503);
    expect(await jobs.job()).toBeUndefined(); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('rejects albums and malformed update ids before persistence', async () => {
    expect((await worker.fetch(req({...photo, message: {...photo.message, media_group_id: 'album'}}), env, ctx)).status).toBe(400);
    expect((await worker.fetch(req({...photo, update_id: -1}), env, ctx)).status).toBe(400); expect(jobs.objects.size).toBe(0);
  });
  it('rejects an expired callback', async () => {
    expect((await worker.fetch(req(save), env, ctx)).status).toBe(410);
  });
});
describe('draft and cost', () => {
  it('selects the largest photo at most 1280 on its longest edge', () => {
    expect(selectPhoto(photo.message.photo).file_id).toBe('fixture-medium'); expect(() => selectPhoto([photo.message.photo[2]])).toThrow();
  });
  it('uses Responses strict schema, low effort, no storage and a downloaded data URL', async () => {
    await draft(); const ai = requests.find(x => x.url.includes('api.openai.com'))!;
    expect(ai.body).toMatchObject({model: 'gpt-6-luna', reasoning: {effort: 'low'}, store: false,
      text: {format: {type: 'json_schema', strict: true, schema: {properties: {rotation: {enum: [0, 90, 180, 270]}}}}}});
    expect(ai.body.input[0].content.find((x: any) => x.type === 'input_image').image_url).toBe('data:image/jpeg;base64,/9j/2Q==');
    expect(JSON.stringify(ai.body)).not.toContain('fixture-only'); expect(ai.init?.signal).toBeInstanceOf(AbortSignal);
    expect(await jobs.job()).toMatchObject({status: 'awaiting_confirmation', card});
    expect(requests.find(x => x.url.endsWith('/sendMessage'))?.body.reply_markup.inline_keyboard[0]).toEqual([
      {text: '저장', callback_data: 'save:900001'}, {text: '취소', callback_data: 'cancel:900001'},
    ]);
    expect(console.log).toHaveBeenCalledWith('openai_usage', expect.objectContaining({costKrw: 0.33375}));
  });
  it('calculates KRW and blocks expensive reservations before OpenAI', async () => {
    expect(estimateCostKrw(1000, 300, 1335)).toBeCloseTo(0.33375); env.MAX_COST_KRW = '0.01'; await draft();
    expect(requests.some(x => x.url.includes('api.openai.com'))).toBe(false); expect(await jobs.job()).toMatchObject({status: 'failed', error: 'cost_limit'});
  });
  it('fails closed for invalid cost settings', async () => {
    env.KRW_PER_USD = 'NaN'; await draft(); expect(requests.some(x => x.url.includes('api.openai.com'))).toBe(false); expect(await jobs.job()).toMatchObject({status: 'failed'});
  });
  it('records failures and asks for a new photo without logging raw errors', async () => {
    fetchMock.mockImplementationOnce(async () => { throw new Error('sensitive detail'); }); await draft();
    expect(await jobs.job()).toMatchObject({status: 'failed'});
    expect(requests.find(x => x.url.endsWith('/sendMessage'))?.body.text).toBe('처리하지 못했습니다. 사진을 다시 보내주세요.');
    expect(JSON.stringify(jobs.entry().storage.puts)).not.toContain('sensitive detail');
  });
  it('rejects malformed model output', async () => {
    const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => String(url).includes('api.openai.com') ? Response.json({status: 'completed', usage: {input_tokens: 1, output_tokens: 1},
      output: [{content: [{type: 'output_text', text: '{"name":"incomplete"}'}]}]}) : previous(url, init));
    await draft(); expect(await jobs.job()).toMatchObject({status: 'failed'});
  });
  it('uses exactly a 25 second abort signal for the model call', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout'); await draft(); expect(timeout).toHaveBeenCalledWith(25_000);
  });
  it('fails closed when usage is missing from a completed model response', async () => {
    const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => String(url).includes('api.openai.com') ? Response.json({status: 'completed',
      output: [{content: [{type: 'output_text', text: JSON.stringify(card)}]}]}) : previous(url, init));
    await draft(); expect(await jobs.job()).toMatchObject({status: 'failed'});
  });
  it('rejects a usage cost above the limit after logging it', async () => {
    const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => String(url).includes('api.openai.com') ? Response.json({status: 'completed',
      usage: {input_tokens: 100000, output_tokens: 300}, output: [{content: [{type: 'output_text', text: JSON.stringify(card)}]}]}) : previous(url, init));
    await draft(); expect(await jobs.job()).toMatchObject({status: 'failed', error: 'cost_limit'});
  });
});
describe('save state machine', () => {
  it('confirms durable saving before People API and creates once', async () => {
    await draft(); const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      if (String(url).includes('people.googleapis.com')) expect(await jobs.job()).toMatchObject({status: 'saving', callbackUpdateId: 900002});
      return previous(url, init);
    });
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'saved', resourceName: 'people/fixture'});
    const statuses = jobs.entry().storage.puts.map(x => x.status);
    expect(statuses).toEqual(['queued', 'drafting', 'awaiting_confirmation', 'awaiting_confirmation', 'saving', 'saved']);
    await confirm(); expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
    expect(requests.find(x => x.url.includes('people.googleapis.com'))?.body).toEqual(buildPerson(card));
  });
  it('maps only writable People API fields', () => {
    expect(buildPerson(card)).toEqual({names: [{givenName: '시험 사용자'}], organizations: [{name: '예시 회사', title: '영업팀장'}],
      phoneNumbers: [{value: '010-0000-0000', type: 'mobile'}, {value: '02-000-0000', type: 'work'}, {value: '02-000-0001', type: 'workFax'}],
      emailAddresses: [{value: 'test@example.invalid', type: 'work'}], urls: [{value: 'https://example.invalid', type: 'work'}],
      addresses: [{formattedValue: '시험 주소', type: 'work'}], biographies: [{value: '로컬 검증 자료', contentType: 'TEXT_PLAIN'}]});
  });
  it('DRY_RUN_SAVE skips OAuth and People', async () => {
    env.DRY_RUN_SAVE = '1'; await draft(); await confirm();
    expect(requests.some(x => /googleapis/.test(x.url))).toBe(false); expect(await jobs.job()).toMatchObject({status: 'saved', resourceName: 'dry-run'});
  });
  it('cancels once without accessing Google', async () => {
    await draft(); const cancel = {...save, callback_query: {...save.callback_query, data: 'cancel:900001'}};
    await worker.fetch(req(cancel), env, ctx); await drain(); await worker.fetch(req(cancel), env, ctx); await drain();
    expect(await jobs.job()).toMatchObject({status: 'cancelled', callbackUpdateId: 900002});
    expect(requests.some(x => /googleapis/.test(x.url))).toBe(false);
    expect(requests.filter(x => x.body?.text === '취소했습니다.')).toHaveLength(1);
  });
  it('rejects a callback for another chat', async () => {
    await draft(); env.ALLOWED_CHAT_IDS = '424242,777'; const other = structuredClone(save); other.callback_query.message.chat.id = 777;
    expect((await worker.fetch(req(other), env, ctx)).status).toBe(403);
  });
  it('keeps saved if completion notification fails', async () => {
    await draft(); const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => String(url).endsWith('/sendMessage') ? new Response('', {status: 500}) : previous(url, init));
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'saved'});
  });
  it('exchanges the refresh token using OAuth form encoding', async () => {
    await draft(); await confirm(); const oauth = requests.find(x => x.url === 'https://oauth2.googleapis.com/token')!;
    expect(oauth.init?.headers).toMatchObject({'content-type': 'application/x-www-form-urlencoded'});
    expect(oauth.body).toEqual({grant_type: 'refresh_token', client_id: 'fixture-only', client_secret: 'fixture-only', refresh_token: 'fixture-only'});
  });
  it('marks a Google failure unknown and never retries it', async () => {
    await draft(); const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => String(url).includes('people.googleapis.com') ? new Response('', {status: 503}) : previous(url, init));
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'unknown', error: 'save_outcome_unconfirmed'});
    await confirm(); await jobs.restart().alarm(); await drain();
    // The failing request is intercepted above, so assert on the complete fetch mock call list.
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('people.googleapis.com'))).toHaveLength(1);
  });
});
describe('independent review regressions', () => {
  it('prevents two Worker isolates from creating the same contact twice', async () => {
    await draft(); vi.resetModules(); const secondWorker = (await import('../src/index')).default;
    // The Workers have separate module state; both route to the same named job object.
    await Promise.all([worker.fetch(req(save), env, ctx), secondWorker.fetch(req(save), {...env}, ctx)]); await drain();
    expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
    expect(await jobs.job()).toMatchObject({status: 'saved'});
  });
  it('runs save after a callback arrives while draft put completion is delayed', async () => {
    const held = deferred(), entered = deferred(), storage = jobs.entry().storage;
    const original = storage.put.bind(storage);
    vi.spyOn(storage, 'put').mockImplementation(async (key, value) => {
      await original(key, value);
      if (key === 'job' && (value as Job).status === 'awaiting_confirmation' && !(value as Job).pendingAction) {
        entered.resolve(); await held.promise;
      }
    });
    await worker.fetch(req(photo), env, ctx); await entered.promise;
    // Admission queues behind the storage input gate, but it must enqueue a fresh processing turn.
    const callback = worker.fetch(req(save), env, ctx);
    held.resolve(); expect((await callback).status).toBe(200); await drain();
    expect(await jobs.job()).toMatchObject({status: 'saved'});
    expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
  });
  it('remembers an early callback accepted while OCR is pending', async () => {
    const held = deferred(), entered = deferred(), previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      if (String(url).includes('api.openai.com')) { entered.resolve(); await held.promise; }
      return previous(url, init);
    });
    await worker.fetch(req(photo), env, ctx); await entered.promise;
    expect((await worker.fetch(req(save), env, ctx)).status).toBe(200);
    expect(await jobs.entry().storage.get('update:900002')).toBe(true);
    held.resolve(); await drain(); expect(await jobs.job()).toMatchObject({status: 'saved'});
  });
  it('deduplicates simultaneous photos through one object', async () => {
    await Promise.all([worker.fetch(req(photo), env, ctx), worker.fetch(req(photo), env, ctx)]); await drain();
    expect(jobs.entry().storage.puts.filter(x => x.status === 'queued')).toHaveLength(1);
    expect(requests.filter(x => x.url.includes('api.openai.com'))).toHaveLength(1);
  });
  it('handles different concurrent save update ids with only one create', async () => {
    await draft();
    await Promise.all([worker.fetch(req(save), env, ctx), worker.fetch(req({...save, update_id: 900004}), env, ctx)]); await drain();
    expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
    expect(await jobs.entry().storage.get('update:900002')).toBe(true);
    expect(await jobs.entry().storage.get('update:900004')).toBe(true);
  });
  it('preserves the first accepted decision when cancel and save race', async () => {
    await draft();
    const cancel = {...save, callback_query: {...save.callback_query, data: 'cancel:900001'}};
    await Promise.all([worker.fetch(req(cancel), env, ctx), worker.fetch(req({...save, update_id: 900004}), env, ctx)]); await drain();
    expect(await jobs.job()).toMatchObject({status: 'cancelled'}); expect(requests.some(x => /googleapis/.test(x.url))).toBe(false);
  });
});
describe('durable alarm recovery', () => {
  it('recovers queued work after object restart', async () => {
    jobs.entry(queued.id).storage.data.set('job', {...queued, createdAt: Date.now(), expiresAt: Date.now() + 604800000});
    await jobs.restart(queued.id).alarm(); await drain();
    expect(await jobs.job(queued.id)).toMatchObject({status: 'awaiting_confirmation'});
  });
  it('recovers a pending save decision after object restart', async () => {
    seed('awaiting_confirmation', {pendingAction: {action: 'save', updateId: 900002}});
    await jobs.restart().alarm(); await drain(); expect(await jobs.job()).toMatchObject({status: 'saved'});
    expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
  });
  it('turns interrupted saving into unknown and guides the user to Google Contacts', async () => {
    seed('saving', {deadline: Date.now() - 1});
    await jobs.restart().alarm(); await drain(); expect(await jobs.job()).toMatchObject({status: 'unknown', error: 'save_outcome_unconfirmed'});
    expect(requests.find(x => x.body?.text?.includes('Google 주소록'))?.body.text).toContain('다시 저장하지 말고');
    await confirm(); await jobs.restart().alarm(); await drain(); expect(requests.some(x => /googleapis/.test(x.url))).toBe(false);
  });
  it('rearms saving watchdog without repeating a save before the deadline', async () => {
    const deadline = Date.now() + 60000; seed('saving', {deadline});
    await jobs.restart().alarm(); expect(await jobs.job()).toMatchObject({status: 'saving'});
    expect(jobs.entry().storage.alarm).toBe(deadline); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('leaves saving after Google succeeds but persistence fails, then alarm resolves unknown', async () => {
    await draft(); const storage = jobs.entry().storage, original = storage.put.bind(storage);
    vi.spyOn(storage, 'put').mockImplementation(async (key, value) => {
      if (key === 'job' && (value as Job).status === 'saved') throw new Error('unavailable');
      return original(key, value);
    });
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'saving'}); expect(storage.alarm).not.toBeNull();
    const job = (await jobs.job())!; job.deadline = Date.now() - 1; storage.data.set('job', job);
    await jobs.restart().alarm(); await confirm();
    expect(await jobs.job()).toMatchObject({status: 'unknown'});
    expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
  });
  it('does not create when alarm expires saving during OAuth', async () => {
    await draft(); const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      const response = await previous(url, init);
      if (String(url).includes('oauth2.googleapis.com')) {
        const job = (await jobs.job())!; job.deadline = Date.now() - 1; jobs.entry().storage.data.set('job', job);
        await jobs.entry().object.alarm();
      }
      return response;
    });
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'unknown'});
    expect(requests.some(x => x.url.includes('people.googleapis.com'))).toBe(false);
  });
  it('keeps unknown when a late People response arrives after alarm timeout', async () => {
    await draft(); const previous = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      const response = await previous(url, init);
      if (String(url).includes('people.googleapis.com')) {
        const job = (await jobs.job())!; job.deadline = Date.now() - 1; jobs.entry().storage.data.set('job', job);
        await jobs.entry().object.alarm();
      }
      return response;
    });
    await confirm(); expect(await jobs.job()).toMatchObject({status: 'unknown'});
    await confirm(); expect(requests.filter(x => x.url.includes('people.googleapis.com'))).toHaveLength(1);
  });
  it('marks interrupted drafting failed without repeating OpenAI', async () => {
    seed('drafting', {deadline: Date.now() - 1}); await jobs.restart().alarm();
    expect(await jobs.job()).toMatchObject({status: 'failed', error: 'draft_interrupted'});
    expect(requests.some(x => x.url.includes('api.openai.com'))).toBe(false);
  });
  it('keeps a new job alarm when expiry cleanup overlaps a fresh photo', async () => {
    seed('saved', {expiresAt: Date.now() - 1});
    const held = deferred(), entered = deferred(), storage = jobs.entry().storage;
    const original = storage.deleteAll.bind(storage);
    vi.spyOn(storage, 'deleteAll').mockImplementation(async () => {
      await original(); entered.resolve(); await held.promise;
    });
    const cleanup = jobs.entry().object.alarm(); await entered.promise;
    let admitted = false;
    const incoming = worker.fetch(req(photo), env, ctx).then(response => { admitted = true; return response; });
    await new Promise(resolve => setTimeout(resolve, 0));
    if (admitted) await drain();
    held.resolve(); await cleanup; expect((await incoming).status).toBe(200); await drain();
    expect(await jobs.job()).toMatchObject({status: 'awaiting_confirmation'});
    expect(storage.alarm).not.toBeNull();
  });
  it('expires PII and update receipts together after seven days', async () => {
    seed('saved', {expiresAt: Date.now() - 1}); jobs.entry().storage.data.set('update:900001', true);
    await jobs.restart().alarm(); expect(await jobs.job()).toBeUndefined();
    expect(jobs.entry().storage.data.size).toBe(0); expect(jobs.entry().storage.alarm).toBeNull();
  });
});
describe('offline transport', () => {
  it('LOCAL_MOCK completes without global fetch', async () => {
    env.LOCAL_MOCK = '1'; env.ENVIRONMENT = 'local'; env.DRY_RUN_SAVE = '1'; await draft(); await confirm();
    expect(await jobs.job()).toMatchObject({status: 'saved', resourceName: 'dry-run'}); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('refuses mock mode outside the local environment', async () => {
    env.LOCAL_MOCK = '1'; env.ENVIRONMENT = 'production'; expect((await worker.fetch(req(photo), env, ctx)).status).toBe(503); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('hides the status endpoint outside local mock mode', async () => {
    seed('saved'); const request = new Request('http://localhost/__local/jobs/900001');
    expect((await worker.fetch(request, env, ctx)).status).toBe(404);
    env.ENVIRONMENT = 'local'; env.LOCAL_MOCK = '1';
    const response = await worker.fetch(request, env, ctx);
    expect(await response.json()).toEqual({id: '900001', status: 'saved'});
  });
});
