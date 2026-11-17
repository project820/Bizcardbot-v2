export interface Env {
  PAIRINGS: DurableObjectNamespace;
  MANAGER_BOT_TOKEN: string;
  MANAGER_WEBHOOK_SECRET: string;
  MANAGER_BOT_USERNAME: string;
  TELEGRAM_IP_CHECK?: string;
}
export type Transport = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type Status = 'pending' | 'started' | 'created' | 'claimed' | 'expired';
interface Pairing {
  expiresAt: number;
  purgeAt: number;
  status: Status;
  linkId?: string;
  botName?: string;
  suggestedUsername?: string;
  requestId?: number;
  ownerId?: number;
  botId?: number;
  botUsername?: string;
  codeHash?: string;
  codeSalt?: string;
  failedAttempts?: number;
  delivered?: boolean;
}
interface User {id: number; is_bot: boolean; username?: string}
interface TelegramUpdate {
  managed_bot?: {user?: User; bot?: User};
  message?: {from?: User; chat?: {id: number; type: string}; text?: string; managed_bot_created?: {bot?: User}};
}
interface Expiring {expiresAt: number}
interface Rate extends Expiring {count: number}
interface Index extends Expiring {pollHash: string}
interface Forwarded {body: Record<string, unknown>; pollHash?: string; ipHash?: string}
const TTL = 600_000;
const RECEIPT_TTL = 86_400_000;
const BODY_TIMEOUT = 5000;
const encoder = new TextEncoder();
function json(body: unknown, status = 200): Response {
  return Response.json(body, {status, headers: {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}});
}
function missing(): Response {return json({error: '찾을 수 없습니다.'}, 404);}
function random(bytes: number): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
function randomInt(limit: number): number {
  const ceiling = Math.floor(0x100000000 / limit) * limit;
  let value: number;
  do {value = crypto.getRandomValues(new Uint32Array(1))[0];} while (value >= ceiling);
  return value % limit;
}
async function hash(value: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value))), b=>b.toString(16).padStart(2,'0')).join('');
}
// Web Crypto verifies MACs without a JS comparison of secret strings.
async function constantTimeEqual(expected: string, supplied: string): Promise<boolean> {
  const key = await crypto.subtle.importKey('raw', crypto.getRandomValues(new Uint8Array(32)), {name:'HMAC',hash:'SHA-256'}, false, ['sign','verify']);
  const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(expected));
  return crypto.subtle.verify('HMAC', key, signature, encoder.encode(supplied));
}
class InputError extends Error {constructor(readonly status: number) {super('입력 오류');}}
function failure(error: unknown): Response {
  return error instanceof InputError ? json({error:'요청 내용을 확인하세요.'},error.status) : json({error:'잠시 후 다시 시도하세요.'},503);
}
async function readJson(request: Request, maxBytes: number): Promise<Record<string, unknown>> {
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new InputError(415);
  if (Number(request.headers.get('content-length')) > maxBytes) throw new InputError(413);
  const reader = request.body?.getReader();
  if (!reader) throw new InputError(400);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_,reject)=>{
    timer = setTimeout(()=>reject(new InputError(408)),BODY_TIMEOUT);
  });
  try {
    const chunks: Uint8Array[] = []; let size = 0;
    while (true) {
      const {value,done} = await Promise.race([reader.read(),deadline]);
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) throw new InputError(413);
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) {bytes.set(chunk,offset); offset += chunk.byteLength;}
    let body: unknown;
    try {body = JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(bytes));}
    catch {throw new InputError(400);}
    if (!record(body)) throw new InputError(400);
    return body;
  } catch (error) {
    // An uncooperative stream's cancel promise must not delay the error response.
    void reader.cancel().catch(()=>{});
    throw error;
  } finally {if (timer !== undefined) clearTimeout(timer);}
}
function record(value: unknown): value is Record<string, unknown> {return value !== null && typeof value === 'object' && !Array.isArray(value);}
function validUser(user: User | undefined, isBot: boolean): user is User {
  return !!user && Number.isSafeInteger(user.id) && user.id > 0 && user.is_bot === isBot;
}
function telegramIp(ip: string): boolean {
  if (!/^(?:\d{1,3}\.){3}\d{1,3}$/.test(ip)) return false;
  const [a,b,c,d] = ip.split('.').map(Number);
  if ([a,b,c,d].some(n=>n>255)) return false;
  return (a===149 && b===154 && c>=160 && c<=175) || (a===91 && b===108 && c>=4 && c<=7);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const path = new URL(request.url).pathname;
      if (path === '/' && request.method === 'GET') return new Response('명함봇 설치 도우미입니다. 설치기에서 시작하세요.', {headers:{'content-type':'text/plain; charset=utf-8'}});
      const create = path === '/v1/pairings' && request.method === 'POST';
      const action = /^\/v1\/pairings\/(status|claim|ack)$/.test(path) && request.method === 'POST';
      const webhook = path === '/telegram/webhook' && request.method === 'POST';
      if (!create && !action && !webhook) return missing();
      const forwarded: Partial<Forwarded> = {};
      if (webhook) {
        if (!env.MANAGER_WEBHOOK_SECRET) return json({error:'관리 봇 설정이 필요합니다.'},503);
        if (!(await constantTimeEqual(env.MANAGER_WEBHOOK_SECRET, request.headers.get('X-Telegram-Bot-Api-Secret-Token') ?? ''))) return json({error:'인증하지 못했습니다.'},401);
        if (env.TELEGRAM_IP_CHECK !== '0' && !telegramIp(request.headers.get('CF-Connecting-IP') ?? '')) return json({error:'허용되지 않은 발신 주소입니다.'},403);
      }
      if (action) {
        const bearer = request.headers.get('Authorization')?.match(/^Bearer ([A-Za-z0-9_-]{43,512})$/)?.[1];
        if (!bearer) return missing();
        forwarded.pollHash = await hash(bearer);
      }
      if (!env.MANAGER_BOT_TOKEN || !/^[A-Za-z0-9_]{5,32}$/.test(env.MANAGER_BOT_USERNAME)) return json({error:'관리 봇 설정이 필요합니다.'},503);
      // Consume and validate the entire external stream BEFORE touching the global DO.
      // The DO receives only a freshly serialized, parsed envelope, never that stream.
      forwarded.body = await readJson(request,webhook ? 65_536 : 1024);
      if (create) forwarded.ipHash = await hash(request.headers.get('CF-Connecting-IP') ?? 'unknown');
      const stub = env.PAIRINGS.get(env.PAIRINGS.idFromName('pairings-v1'));
      return await stub.fetch(new Request(`https://pairings.internal${path}`, {
        method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(forwarded),
      }));
    } catch (error) {
      // Never log raw requests, API URLs, errors, pairing IDs, codes or tokens.
      return failure(error);
    }
  },
};

export class PairingsObject {
  constructor(private readonly state: DurableObjectState, private readonly env: Env, private readonly transport: Transport = fetch) {}

  async fetch(request: Request): Promise<Response> {
    // Even the trusted buffered envelope is parsed outside the serialized state gate.
    let envelope: Forwarded;
    try {envelope = await request.json() as Forwarded; if (!record(envelope) || !record(envelope.body)) throw new InputError(400);}
    catch (error) {return failure(error);}
    return this.state.blockConcurrencyWhile(async () => {
      try {
        const path = new URL(request.url).pathname;
        if (path === '/v1/pairings') return await this.create(envelope.body,envelope.ipHash!);
        if (path === '/telegram/webhook') {await this.update(envelope.body as TelegramUpdate); return json({ok:true});}
        if (/^\/v1\/pairings\/(status|claim|ack)$/.test(path)) return await this.action(path.split('/').at(-1)!,envelope);
        return missing();
      } catch (error) {return failure(error);}
    });
  }

  private async arm(at: number): Promise<void> {
    const current = await this.state.storage.getAlarm();
    if (current === null || current > at) await this.state.storage.setAlarm(at);
  }

  private async create(body: Record<string,unknown>, ipHash: string): Promise<Response> {
    if (typeof body.bot_name !== 'string' || !body.bot_name.trim() || [...body.bot_name].length > 64 || /[\u0000-\u001f\u007f]/.test(body.bot_name)) throw new InputError(400);
    const now = Date.now(), rateKey = `rate:${ipHash}`;
    const current = await this.state.storage.get<Rate>(rateKey);
    const rate = current && current.expiresAt > now ? current : {count:0,expiresAt:now+TTL};
    if (rate.count >= 10) return json({error:'요청이 많습니다. 10분 뒤 다시 시도하거나 설치기 --manual을 사용하세요.'},429);
    rate.count++;
    const id = random(24), token = random(32), pollHash = await hash(token);
    const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
    const suffix = Array.from({length:6},()=>alphabet[randomInt(alphabet.length)]).join('');
    const pair: Pairing = {expiresAt:now+TTL,purgeAt:now+TTL+RECEIPT_TTL,status:'pending',linkId:id,botName:body.bot_name.trim(),
      suggestedUsername:`bizcard_${suffix}_bot`,requestId:crypto.getRandomValues(new Int32Array(1))[0]};
    await this.arm(pair.expiresAt);
    await this.state.storage.transaction(async tx=>{
      await tx.put(rateKey,rate);
      await tx.put(`pair:${pollHash}`,pair);
      await tx.put(`link:${id}`,{pollHash,expiresAt:pair.expiresAt});
    });
    return json({poll_token:token,suggested_username:pair.suggestedUsername,
      deep_link:`https://t.me/${this.env.MANAGER_BOT_USERNAME}?start=${id}`,expires_at:Math.floor(pair.expiresAt/1000)},201);
  }

  private async retire(pollHash: string, pair: Pairing, status: 'claimed' | 'expired'): Promise<void> {
    await this.state.storage.transaction(async tx=>{
      await tx.put(`pair:${pollHash}`,{status,expiresAt:pair.expiresAt,purgeAt:pair.purgeAt});
      if (pair.linkId) await tx.delete(`link:${pair.linkId}`);
      if (pair.ownerId) {
        const owner = await tx.get<Index>(`owner:${pair.ownerId}`);
        if (owner?.pollHash === pollHash) await tx.delete(`owner:${pair.ownerId}`);
      }
    });
  }

  private async action(action: string, {pollHash,body}: Forwarded): Promise<Response> {
    if (!pollHash) return missing();
    const key = `pair:${pollHash}`, pair = await this.state.storage.get<Pairing>(key);
    if (!pair) return missing();
    if (pair.status === 'claimed' || pair.status === 'expired') return json({status:pair.status},410);
    if (pair.expiresAt <= Date.now()) {await this.retire(pollHash,pair,'expired'); return json({status:'expired'},410);}
    if (action === 'status') return json({status:pair.status});
    if (pair.status !== 'created') return json({status:pair.status},409);
    if (action === 'ack') {
      if (!pair.delivered) return json({status:pair.status},409);
      await this.retire(pollHash,pair,'claimed');
      try {await this.api('sendMessage',{chat_id:pair.ownerId,text:`완료: @${pair.botUsername} 에서 명함 사진을 보내세요`,reply_markup:{remove_keyboard:true}});}
      catch { /* A notification failure must not restore personal data after ack. */ }
      return json({status:'claimed'});
    }
    const supplied = typeof body.code === 'string' && /^\d{6}$/.test(body.code) ? body.code : '';
    if (!supplied || !(await constantTimeEqual(pair.codeHash!,await hash(`${pair.codeSalt}:${supplied}`)))) {
      pair.failedAttempts = (pair.failedAttempts ?? 0) + 1;
      if (pair.failedAttempts >= 5) {await this.retire(pollHash,pair,'expired'); return json({status:'expired'},410);}
      await this.state.storage.put(key,pair);
      return json({status:'bad_code',attempts_left:5-pair.failedAttempts},403);
    }
    // Tokens exist only in this request's memory and the TLS response, never in storage.
    const token = await this.api('getManagedBotToken',{user_id:pair.botId});
    if (typeof token !== 'string' || !token) throw new Error('관리 봇 토큰 응답 오류');
    let accessRestricted = false;
    try {accessRestricted = await this.api('setManagedBotAccessSettings',{user_id:pair.botId,is_access_restricted:true}) === true;}
    catch { /* Best effort; the installer receives the actual result. */ }
    if (pair.expiresAt <= Date.now()) {await this.retire(pollHash,pair,'expired'); return json({status:'expired'},410);}
    pair.delivered = true;
    await this.state.storage.put(key,pair);
    return json({status:'ready',bot_token:token,bot_id:pair.botId,bot_username:pair.botUsername,owner_user_id:pair.ownerId,access_restricted:accessRestricted});
  }

  private async api(method: string, body: unknown): Promise<unknown> {
    const response = await this.transport(`https://api.telegram.org/bot${this.env.MANAGER_BOT_TOKEN}/${method}`, {
      method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(5000),redirect:'error',
    });
    const data: unknown = await response.json();
    if (!response.ok || !record(data) || data.ok !== true) throw new Error('관리 봇 요청 실패');
    return data.result;
  }

  private async update(update: TelegramUpdate): Promise<void> {
    const message = update.message;
    if (message?.chat?.type === 'private' && validUser(message.from,false) && message.chat.id === message.from.id) {
      const id = typeof message.text === 'string' ? message.text.match(/^\/start(?:@[A-Za-z0-9_]+)? ([A-Za-z0-9_-]{1,64})$/)?.[1] : undefined;
      if (id) {await this.start(id,message.from.id); return;}
      if (message.managed_bot_created) {await this.managed(message.from,message.managed_bot_created.bot); return;}
    }
    if (update.managed_bot) await this.managed(update.managed_bot.user,update.managed_bot.bot,true);
  }

  private async start(id: string, ownerId: number): Promise<void> {
    const link = await this.state.storage.get<Index>(`link:${id}`);
    if (!link) return;
    const key = `pair:${link.pollHash}`, pair = await this.state.storage.get<Pairing>(key);
    if (!pair || pair.expiresAt <= Date.now()) return;
    if (pair.ownerId && pair.ownerId !== ownerId) {
      await this.api('sendMessage',{chat_id:ownerId,text:'이미 사용된 링크입니다. 내 설치 화면에서 새 링크를 만드세요.'}); return;
    }
    if (pair.status !== 'pending' && pair.status !== 'started') return;
    const ownerKey = `owner:${ownerId}`;
    if (pair.status === 'pending') {
      const previous = await this.state.storage.get<Index>(ownerKey);
      if (previous && previous.pollHash !== link.pollHash) {
        const old = await this.state.storage.get<Pairing>(`pair:${previous.pollHash}`);
        if (old && old.status !== 'claimed') await this.retire(previous.pollHash,old,'expired');
      }
      pair.ownerId = ownerId; pair.status = 'started';
      await this.state.storage.transaction(async tx=>{
        await tx.put(key,pair); await tx.put(ownerKey,{pollHash:link.pollHash,expiresAt:pair.expiresAt});
      });
    }
    await this.api('sendMessage',{chat_id:ownerId,text:'내 컴퓨터의 설치 화면에서 연 링크인지 확인하세요. 아래 버튼으로 명함봇을 만드세요. 링크 생성 후 10분 안에 설치를 마치세요.',reply_markup:{
      resize_keyboard:true,keyboard:[[{text:'내 명함봇 만들기',request_managed_bot:{request_id:pair.requestId,suggested_name:pair.botName,suggested_username:pair.suggestedUsername}}]],
    }});
    await this.api('sendMessage',{chat_id:ownerId,text:'버튼이 보이지 않으면 텔레그램을 업데이트하거나 아래 링크를 누르세요.',reply_markup:{inline_keyboard:[[
      {text:'버튼이 안 보이면 여기를 누르세요',url:`https://t.me/newbot/${this.env.MANAGER_BOT_USERNAME}/${pair.suggestedUsername}?name=${encodeURIComponent(pair.botName!)}`},
    ]]}});
  }

  private async managed(user: User | undefined, bot: User | undefined, requireSuggestedUsername = false): Promise<void> {
    if (!validUser(user,false) || !validUser(bot,true) || typeof bot.username !== 'string' || !/^[A-Za-z0-9_]{5,32}$/.test(bot.username)) return;
    const owner = await this.state.storage.get<Index>(`owner:${user.id}`);
    if (!owner || owner.expiresAt <= Date.now()) return;
    const key = `pair:${owner.pollHash}`, pair = await this.state.storage.get<Pairing>(key);
    if (!pair || pair.status !== 'started' || pair.ownerId !== user.id || pair.expiresAt <= Date.now()) return;
    if (requireSuggestedUsername && bot.username.toLowerCase() !== pair.suggestedUsername?.toLowerCase()) return;
    const code = randomInt(1_000_000).toString().padStart(6,'0'), salt = random(16);
    const codeHash = await hash(`${salt}:${code}`);
    // Send before committing created: a failed notification can be retried by Telegram
    // without storing a recoverable copy of the code. State gates serialize duplicates.
    await this.api('sendMessage',{chat_id:user.id,text:`설치 화면에 이 코드를 입력하세요: ${code}\n다른 사람에게 코드를 알려주면 그 사람이 내 봇을 조종할 수 있습니다.`});
    if (pair.expiresAt <= Date.now()) {await this.retire(owner.pollHash,pair,'expired'); return;}
    await this.state.storage.put(key,{...pair,status:'created',botId:bot.id,botUsername:bot.username,codeHash,codeSalt:salt,failedAttempts:0});
  }

  async alarm(): Promise<void> {
    return this.state.blockConcurrencyWhile(async ()=>{
      const now = Date.now(); let next = Infinity;
      const entries = await this.state.storage.list<Pairing | Expiring>();
      for (const [key,value] of entries) {
        if (key.startsWith('pair:')) {
          const pair = value as Pairing;
          if (pair.purgeAt <= now) {
            await this.retire(key.slice(5),pair,'expired');
            await this.state.storage.delete(key); continue;
          }
          if (pair.expiresAt <= now) {
            if (pair.status !== 'claimed' && pair.status !== 'expired') await this.retire(key.slice(5),pair,'expired');
            next = Math.min(next,pair.purgeAt);
          } else next = Math.min(next,pair.expiresAt);
        } else if (value.expiresAt <= now) await this.state.storage.delete(key);
        else next = Math.min(next,value.expiresAt);
      }
      if (Number.isFinite(next)) await this.state.storage.setAlarm(next);
      else await this.state.storage.deleteAlarm();
    });
  }
}
