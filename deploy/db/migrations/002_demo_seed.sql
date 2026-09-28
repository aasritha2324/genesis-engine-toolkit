-- Optional seed for the demo flow. Additive only: never overwrites data.
-- Demo login: demo@advanta.dev / advanta-demo-2026
INSERT INTO advertisers (id, name)
VALUES ('11111111-1111-1111-1111-111111111111', 'AdVanta Demo Co')
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (id, email, password_hash, advertiser_id)
VALUES ('22222222-2222-2222-2222-222222222222', 'demo@advanta.dev',
        '$2b$12$Umhp5A5SNF3rOPp.4oSHouDHxwM2T9Obl1x4l1EEVS.oWeQyD5SW.',
        '11111111-1111-1111-1111-111111111111')
ON CONFLICT (email) DO NOTHING;

INSERT INTO user_roles (user_id, role)
VALUES ('22222222-2222-2222-2222-222222222222', 'advertiser')
ON CONFLICT (user_id, role) DO NOTHING;

INSERT INTO campaigns (id, advertiser_id, name)
VALUES ('camp_demo001', '11111111-1111-1111-1111-111111111111', 'Spring Launch'),
       ('camp_demo002', '11111111-1111-1111-1111-111111111111', 'Retarget EU')
ON CONFLICT (id) DO NOTHING;

INSERT INTO ads (id, campaign_id, advertiser_id, title)
VALUES ('ad_demo001', 'camp_demo001', '11111111-1111-1111-1111-111111111111', 'Spring Sale Hero'),
       ('ad_demo002', 'camp_demo001', '11111111-1111-1111-1111-111111111111', 'Bundle Promo'),
       ('ad_demo003', 'camp_demo002', '11111111-1111-1111-1111-111111111111', 'Cart Reminder')
ON CONFLICT (id) DO NOTHING;
