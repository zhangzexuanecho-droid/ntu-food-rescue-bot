-- =============================================================
-- 校园食物剩余再分配 Telegram Bot — PostgreSQL Schema（Supabase）
-- 使用方法：Supabase Dashboard → SQL Editor → 粘贴并 Run
-- =============================================================

-- -------------------------------------------------------------
-- 1. stalls：参与项目的食堂摊位（商家）
-- -------------------------------------------------------------
create table if not exists public.stalls (
    id                 uuid primary key default gen_random_uuid(),
    stall_name         text not null,
    canteen_location   text not null,
    -- 绑定商家 Telegram 账号；vendor bot 只显示该 vendor 名下的摊位
    vendor_telegram_id bigint
);

-- -------------------------------------------------------------
-- 2. listings：摊位发布的剩余食物
-- -------------------------------------------------------------
create table if not exists public.listings (
    id             uuid primary key default gen_random_uuid(),
    stall_id       uuid not null references public.stalls (id) on delete cascade,
    title          text not null,
    original_price numeric(10, 2) not null check (original_price >= 0),
    discount_price numeric(10, 2) not null check (discount_price >= 0 and discount_price <= original_price),
    quantity       integer not null default 0 check (quantity >= 0),
    -- 商家提前下架后为 true：超时释放、预订失败回补库存都不会再把它放出来
    stopped        boolean not null default false,
    -- 发布时挂了几份（quantity 会随预订、下架变化），用于统计 listed vs collected
    initial_quantity integer check (initial_quantity >= 0),
    pickup_time    text,
    -- 注意：原需求为 timestamp，此处改用 timestamptz 以正确存储时区信息
    -- （Supabase 官方约定）。如必须使用 timestamp，把类型改回即可。
    created_at     timestamptz not null default now()
);

-- -------------------------------------------------------------
-- 3. reservations：学生领取预订记录
-- -------------------------------------------------------------
create table if not exists public.reservations (
    id                  uuid primary key default gen_random_uuid(),
    listing_id          uuid not null references public.listings (id) on delete cascade,
    student_telegram_id bigint not null,
    claim_code          text not null unique,
    status              text not null default 'pending'
                        check (status in ('pending', 'claimed', 'completed', 'cancelled')),
    created_at          timestamptz not null default now()
);

-- 外键列上的索引（Postgres 不会自动为外键建索引）
create index if not exists idx_stalls_vendor_telegram_id  on public.stalls (vendor_telegram_id);
create index if not exists idx_listings_stall_id          on public.listings (stall_id);
create index if not exists idx_reservations_listing_id    on public.reservations (listing_id);
create index if not exists idx_reservations_telegram_id   on public.reservations (student_telegram_id);

-- -------------------------------------------------------------
-- 示例数据：3 个 NTU 食堂摊位
-- （注意：重复运行本段会重复插入数据）
-- -------------------------------------------------------------
insert into public.stalls (stall_name, canteen_location) values
    ('Western Food @ Canteen 2', 'Canteen 2'),
    ('Chicken Rice @ North Spine', 'North Spine Plaza'),
    ('Mini Wok @ Canteen 11', 'Canteen 11');

-- 每个摊位一条示例 listing（通过 stall_name 反查 uuid）
-- 鸡扒饭挂了 5 份、下面有 1 条示例预订，所以剩余 4 份
insert into public.listings (stall_id, title, original_price, discount_price, quantity, initial_quantity, pickup_time)
select id, 'Grilled Chicken Chop Rice', 7.00, 3.50, 4, 5, '7:30 PM - 8:15 PM'
from public.stalls where stall_name = 'Western Food @ Canteen 2';

insert into public.listings (stall_id, title, original_price, discount_price, quantity, initial_quantity, pickup_time)
select id, 'Steamed Chicken Rice Set', 4.80, 2.00, 10, 10, '7:45 PM - 8:30 PM'
from public.stalls where stall_name = 'Chicken Rice @ North Spine';

insert into public.listings (stall_id, title, original_price, discount_price, quantity, initial_quantity, pickup_time)
select id, 'Beef Hor Fun', 5.50, 2.80, 3, 3, '8:00 PM - 8:30 PM'
from public.stalls where stall_name = 'Mini Wok @ Canteen 11';

-- 示例预订记录（claim_code 唯一，供摊位核销使用）
insert into public.reservations (listing_id, student_telegram_id, claim_code)
select id, 123456789, 'K7Q2XM'
from public.listings where title = 'Grilled Chicken Chop Rice';

-- -------------------------------------------------------------
-- 关于 Row Level Security（RLS）
-- 建表时 RLS 默认关闭。Telegram Bot 后端应使用 service_role key
-- 访问 Supabase（service_role 绕过 RLS），无需额外配置。
-- 若未来要用 anon key 暴露数据给客户端，再启用 RLS 并加 policy，例如：
--   alter table public.listings enable row level security;
--   create policy "public read listings" on public.listings
--       for select using (true);
-- -------------------------------------------------------------
