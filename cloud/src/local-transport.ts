import fixtureCard from '../test/fixtures/card.json';

/** Closed transport for wrangler's local PoC: no branch delegates to fetch. */
export async function localTransport(url: string, _init?: RequestInit): Promise<Response> {
  console.log('local_mock', {operation: new URL(url).pathname.replace(/bot[^/]+/, 'bot[redacted]')});
  if (url === 'https://api.openai.com/v1/responses') {
    return Response.json({status: 'completed', usage: {input_tokens: 1000, output_tokens: 300},
      output: [{content: [{type: 'output_text', text: JSON.stringify(fixtureCard)}]}]});
  }
  if (url.startsWith('https://api.telegram.org/file/')) {
    return new Response(new Uint8Array([255, 216, 255, 217]), {headers: {'content-type': 'image/jpeg'}});
  }
  if (url.endsWith('/getFile')) return Response.json({ok: true, result: {file_path: 'photos/fixture.jpg'}});
  if (url.startsWith('https://api.telegram.org/bot')) return Response.json({ok: true, result: {message_id: 11}});
  if (url === 'https://oauth2.googleapis.com/token') return Response.json({access_token: 'local-mock-token'});
  if (url.startsWith('https://people.googleapis.com/v1/people:createContact')) return Response.json({resourceName: 'people/local-mock'});
  throw new Error('local_transport_rejected');
}
