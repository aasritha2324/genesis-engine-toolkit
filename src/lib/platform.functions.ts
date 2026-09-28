import { createServerFn } from '@tanstack/react-start'
import { requireSupabaseAuth } from '@/integrations/supabase/auth-middleware'
import { z } from 'zod'
import { createHash, createHmac, randomBytes } from 'node:crypto'

const organization = z.object({ name: z.string().trim().min(2).max(100) })
export const setupOrganization = createServerFn({ method: 'POST' }).middleware([requireSupabaseAuth]).validator((input: unknown) => organization.parse(input)).handler(async ({ data, context }) => {
  const { supabaseAdmin } = await import('@/integrations/supabase/client.server')
  const { data: id, error } = await supabaseAdmin.rpc('server_create_advertiser', { _user_id: context.userId, _name: data.name })
  if (error) throw new Error(error.message)
  return id
})

export const getDashboard = createServerFn({ method: 'GET' }).middleware([requireSupabaseAuth]).handler(async ({ context }) => {
  const db = context.supabase
  const [{ data: profile, error: profileError }, { data: roles }, { data: advertisers }, { data: campaigns, error: campaignsError }, { data: ads, error: adsError }, { data: aggregates, error: aggregatesError }, { data: alerts, error: alertsError }, { data: events }] = await Promise.all([
    db.from('profiles').select('user_id,display_name,advertiser_id').eq('user_id', context.userId).maybeSingle(),
    db.from('user_roles').select('role').eq('user_id', context.userId),
    db.from('advertisers').select('id,name'),
    db.from('campaigns').select('id,name,status,advertiser_id,created_at').order('created_at', { ascending: false }),
    db.from('ads').select('id,title,campaign_id,advertiser_id'),
    db.from('aggregated_clicks').select('window_start,click_count,unique_viewers,ad_id,campaign_id,country,device,advertiser_id').gte('window_start', new Date(Date.now()-24*60*60*1000).toISOString()).order('window_start', { ascending: true }).limit(10000),
    db.from('suspicious_clicks').select('id,rule,ad_id,campaign_id,click_count,detected_at,window_start').order('detected_at', { ascending: false }).limit(30),
    db.from('click_events').select('event_id,ad_id,country,device,event_time').order('event_time', { ascending: false }).limit(15),
  ])
  const error = profileError || campaignsError || adsError || aggregatesError || alertsError
  if (error) throw new Error(error.message)
  return { profile, roles: roles ?? [], advertisers: advertisers ?? [], campaigns: campaigns ?? [], ads: ads ?? [], aggregates: aggregates ?? [], alerts: alerts ?? [], events: events ?? [] }
})

export const createCampaign = createServerFn({ method: 'POST' }).middleware([requireSupabaseAuth]).validator((input: unknown) => z.object({ name: z.string().trim().min(2).max(100) }).parse(input)).handler(async ({ data, context }) => {
  const { data: profile } = await context.supabase.from('profiles').select('advertiser_id').eq('user_id',context.userId).single()
  if (!profile?.advertiser_id) throw new Error('Create an organization first')
  const id = `camp_${randomBytes(6).toString('hex')}`
  const { error } = await context.supabase.from('campaigns').insert({ id, name:data.name, advertiser_id:profile.advertiser_id })
  if (error) throw new Error(error.message)
  return id
})
export const createAd = createServerFn({ method: 'POST' }).middleware([requireSupabaseAuth]).validator((input: unknown) => z.object({ title:z.string().trim().min(2).max(100), campaignId:z.string().min(1) }).parse(input)).handler(async ({ data, context }) => {
  const { data: campaign } = await context.supabase.from('campaigns').select('advertiser_id').eq('id',data.campaignId).single()
  if (!campaign) throw new Error('Campaign not found')
  const id = `ad_${randomBytes(6).toString('hex')}`
  const { error } = await context.supabase.from('ads').insert({ id,title:data.title,campaign_id:data.campaignId,advertiser_id:campaign.advertiser_id })
  if (error) throw new Error(error.message)
  return id
})
export const createIngestKey = createServerFn({ method: 'POST' }).middleware([requireSupabaseAuth]).handler(async ({ context }) => {
  const { data: profile } = await context.supabase.from('profiles').select('advertiser_id').eq('user_id', context.userId).single()
  if (!profile?.advertiser_id) throw new Error('Create an organization first')
  const key = `clk_${randomBytes(24).toString('hex')}`
  const { supabaseAdmin } = await import('@/integrations/supabase/client.server')
  const { error } = await supabaseAdmin.from('ingest_keys').insert({ advertiser_id:profile.advertiser_id,label:'Default',key_hash:createHash('sha256').update(key).digest('hex') })
  if (error) throw new Error(error.message)
  return key
})
export const simulateClicks = createServerFn({ method: 'POST' }).middleware([requireSupabaseAuth]).validator((input: unknown) => z.object({ adId:z.string(), count:z.number().int().min(1).max(100), country:z.string().regex(/^[A-Z]{2}$/), device:z.enum(['mobile','desktop','tablet','other']), repeatViewer:z.boolean().default(false) }).parse(input)).handler(async ({ data,context }) => {
  const { data: ad } = await context.supabase.from('ads').select('id').eq('id',data.adId).single()
  if (!ad) throw new Error('Ad not found')
  const secret = process.env['CLICK_IP_HMAC_SECRET']
  if (!secret) throw new Error('Click simulator is unavailable')
  const { supabaseAdmin } = await import('@/integrations/supabase/client.server')
  let accepted=0
  for(let i=0;i<data.count;i++) {
    const viewer = data.repeatViewer ? 'demo-repeat-viewer' : `demo-${randomBytes(8).toString('hex')}`
    const {data: inserted,error} = await supabaseAdmin.rpc('server_ingest_click',{_event_id:`evt_${crypto.randomUUID()}`,_ad_id:data.adId,_viewer_id:createHmac('sha256',secret).update(viewer).digest('hex'),_event_time:new Date().toISOString(),_country:data.country,_device:data.device,...(data.repeatViewer ? {_ip_hash:createHmac('sha256',secret).update('demo-ip').digest('hex')} : {})})
    if(error) throw new Error(error.message)
    if(inserted) accepted++
  }
  return { accepted }
})
