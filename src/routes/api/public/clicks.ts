import { createFileRoute } from '@tanstack/react-router'
import { createHash, createHmac } from 'node:crypto'
import { z } from 'zod'

const click = z.object({ event_id:z.string().min(8).max(120), ad_id:z.string().min(1), viewer_id:z.string().min(1).max(120), timestamp:z.string().datetime({offset:true}), country:z.string().regex(/^[A-Z]{2}$/), device:z.enum(['mobile','desktop','tablet','other']), ip:z.string().optional() })
export const Route = createFileRoute('/api/public/clicks')({
  server: { handlers: { POST: async ({ request }) => {
    const key=request.headers.get('x-api-key')
    if (!key) return Response.json({error:{code:'UNAUTHORIZED',message:'API key required'}},{status:401})
    const { supabaseAdmin }=await import('@/integrations/supabase/client.server')
    const { data: credential }=await supabaseAdmin.from('ingest_keys').select('advertiser_id').eq('key_hash',createHash('sha256').update(key).digest('hex')).is('revoked_at',null).maybeSingle()
    if(!credential) return Response.json({error:{code:'UNAUTHORIZED',message:'Invalid API key'}},{status:401})
    if(request.headers.get('content-length') && Number(request.headers.get('content-length')) > 300000) return Response.json({error:{code:'TOO_LARGE',message:'Batch is too large'}},{status:413})
    let input:unknown
    try { input=await request.json() } catch { return Response.json({error:{code:'INVALID_JSON',message:'Invalid JSON'}},{status:400}) }
    const parsed=z.union([click,z.object({events:z.array(click).min(1).max(500)})]).safeParse(input)
    if(!parsed.success) return Response.json({error:{code:'VALIDATION',message:'Invalid click event'}},{status:422})
    const events='events' in parsed.data ? parsed.data.events : [parsed.data]
    const ids=[...new Set(events.map(e=>e.ad_id))]
    const {data: ads}=await supabaseAdmin.from('ads').select('id,advertiser_id').in('id',ids)
    if(ids.some(id=>!ads?.some(ad=>ad.id===id && ad.advertiser_id===credential.advertiser_id))) return Response.json({error:{code:'NOT_FOUND',message:'Unknown ad'}},{status:404})
    let accepted=0,duplicates=0
    for(const event of events) {
      const secret=process.env['CLICK_IP_HMAC_SECRET']
      if(!secret) return Response.json({error:{code:'UNAVAILABLE',message:'Ingestion unavailable'}},{status:503})
      const ipHash=event.ip ? createHmac('sha256',secret).update(event.ip).digest('hex') : null
      const viewerHash=createHmac('sha256',secret).update(event.viewer_id).digest('hex')
      const {data,error}=await supabaseAdmin.rpc('server_ingest_click',{_event_id:event.event_id,_ad_id:event.ad_id,_viewer_id:viewerHash,_event_time:event.timestamp,_country:event.country,_device:event.device,...(ipHash ? {_ip_hash:ipHash} : {})})
      if(error) return Response.json({error:{code:'INGEST_FAILED',message:error.message}},{status:422})
      if(data) accepted++; else duplicates++
    }
    return Response.json({data:{accepted,duplicates},generated_at:new Date().toISOString()},{status:202,headers:{'X-Served-By':'lovable-edge'}})
  } } },
})
