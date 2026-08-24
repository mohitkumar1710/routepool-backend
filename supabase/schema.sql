-- RoutePool database schema.
--
-- Run this once against your Supabase project: SQL Editor -> New query -> paste
-- -> Run. It is written to be re-runnable (drops precede creates), so you can
-- iterate on it during development. It DESTROYS existing rows in these four
-- tables, so do not run it against anything you care about.
--
-- Auth lives in Supabase's own `auth.users`; `public.profiles` is the app-facing
-- mirror, kept in sync by the trigger at the bottom.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

drop table if exists public.bookings cascade;
drop table if exists public.rides cascade;
drop table if exists public.routes cascade;
drop table if exists public.profiles cascade;

create table public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  name         text not null,
  email        text not null unique,
  role         text not null default 'rider' check (role in ('rider', 'driver', 'both')),
  avatar_url   text,
  -- No reviews table yet, so these stay at their defaults until one exists.
  rating       numeric(2, 1) check (rating between 0 and 5),
  review_count integer not null default 0,
  is_verified  boolean not null default false,
  created_at   timestamptz not null default now()
);

-- OSRM routing results, cached by rounded origin/destination so re-requesting
-- the same trip never calls out to OSRM twice. `route_rank` keeps every
-- alternative OSRM returns for a pair addressable, rather than collapsing them
-- onto one row -- the unique constraint below is over all five columns, not
-- just the origin/destination, on purpose.
create table public.routes (
  id                uuid primary key default gen_random_uuid(),
  -- Rounded to 4 decimal places (~11m) on the way in -- this precision is the
  -- cache key, so a `numeric(7, 4)` column is what actually enforces it.
  origin_lat        numeric(7, 4) not null,
  origin_lng        numeric(7, 4) not null,
  destination_lat   numeric(7, 4) not null,
  destination_lng   numeric(7, 4) not null,
  -- 0 = OSRM's primary route; 1, 2, ... are alternatives, in the order OSRM
  -- returned them.
  route_rank        integer not null default 0 check (route_rank >= 0),
  geometry          text not null,
  distance_meters   integer not null check (distance_meters >= 0),
  duration_seconds  integer not null check (duration_seconds >= 0),
  created_at        timestamptz not null default now(),
  unique (origin_lat, origin_lng, destination_lat, destination_lng, route_rank)
);

create table public.rides (
  -- `from` and `to` are reserved words in SQL as well as Python, so the columns
  -- carry the _location suffix and the API aliases them back on the way out.
  id               uuid primary key default gen_random_uuid(),
  driver_id        uuid not null references public.profiles (id) on delete cascade,
  from_location    text not null,
  to_location      text not null,
  departure_date   date not null,
  departure_time   time not null,
  available_seats  integer not null check (available_seats between 0 and 8),
  price_per_seat   numeric(10, 2) not null check (price_per_seat >= 0),
  vehicle          text not null,
  notes            text,
  origin_lat       numeric(9, 6),
  origin_lng       numeric(9, 6),
  destination_lat  numeric(9, 6),
  destination_lng  numeric(9, 6),
  -- Nullable so rides posted before this column existed don't break.
  route_id         uuid references public.routes (id) on delete set null,
  created_at       timestamptz not null default now()
);

create index rides_driver_idx on public.rides (driver_id);
create index rides_search_idx on public.rides (departure_date, from_location, to_location);
create index rides_route_idx on public.rides (route_id);
create index routes_lookup_idx on public.routes (origin_lat, origin_lng, destination_lat, destination_lng);

create table public.bookings (
  id           uuid primary key default gen_random_uuid(),
  ride_id      uuid not null references public.rides (id) on delete cascade,
  passenger_id uuid not null references public.profiles (id) on delete cascade,
  seats        integer not null check (seats >= 1),
  status       text not null default 'pending' check (status in ('pending', 'confirmed', 'cancelled')),
  created_at   timestamptz not null default now(),
  -- One booking per rider per ride; re-requesting updates the existing row.
  unique (ride_id, passenger_id)
);

create index bookings_ride_idx on public.bookings (ride_id);
create index bookings_passenger_idx on public.bookings (passenger_id);

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- Every query the API makes runs as the calling user (anon key + their JWT), so
-- these policies are the real access control -- not the Python code.
-- ---------------------------------------------------------------------------

alter table public.profiles enable row level security;
alter table public.routes   enable row level security;
alter table public.rides    enable row level security;
alter table public.bookings enable row level security;

-- Profiles are world-readable because ride listings embed their driver. The API
-- strips `email` from everyone except the signed-in user; nothing else in the
-- row is private.
create policy "profiles are readable by everyone"
  on public.profiles for select
  using (true);

create policy "users update their own profile"
  on public.profiles for update
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- Routes are a shared cache, not anyone's data -- world-readable like rides,
-- and any signed-in user can add to the cache. No update/delete policy yet:
-- cached rows are immutable once written.
create policy "routes are readable by everyone"
  on public.routes for select
  using (true);

create policy "authenticated users cache route lookups"
  on public.routes for insert
  to authenticated
  with check (true);

-- Ride search is public (the frontend calls it before sign-in).
create policy "rides are readable by everyone"
  on public.rides for select
  using (true);

create policy "drivers publish their own rides"
  on public.rides for insert
  to authenticated
  with check (auth.uid() = driver_id);

create policy "drivers modify their own rides"
  on public.rides for update
  to authenticated
  using (auth.uid() = driver_id)
  with check (auth.uid() = driver_id);

create policy "drivers delete their own rides"
  on public.rides for delete
  to authenticated
  using (auth.uid() = driver_id);

-- This one policy is what makes `GET /api/bookings` a bare `select *`: it
-- returns exactly the caller's own requests plus every request on a ride they
-- drive, with no filtering needed in Python.
create policy "riders and the ride's driver can read a booking"
  on public.bookings for select
  to authenticated
  using (
    auth.uid() = passenger_id
    or auth.uid() in (select driver_id from public.rides where id = ride_id)
  );

create policy "riders create their own bookings"
  on public.bookings for insert
  to authenticated
  with check (auth.uid() = passenger_id);

create policy "riders cancel their own bookings"
  on public.bookings for update
  to authenticated
  using (auth.uid() = passenger_id)
  with check (auth.uid() = passenger_id);

-- ---------------------------------------------------------------------------
-- New user -> profile row
-- ---------------------------------------------------------------------------

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, name, email, role)
  values (
    new.id,
    coalesce(nullif(new.raw_user_meta_data ->> 'name', ''), split_part(new.email, '@', 1)),
    new.email,
    coalesce(new.raw_user_meta_data ->> 'role', 'rider')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- Accepting / declining a booking
--
-- Status and seat count must move together or the car gets oversold: two
-- confirms racing each other would both read available_seats = 3 and both
-- succeed. Doing it here means one transaction, with the ride row locked, so
-- the second confirm sees the first one's decrement.
-- ---------------------------------------------------------------------------

create or replace function public.set_booking_status(p_booking_id uuid, p_status text)
returns public.bookings
language plpgsql
security definer
set search_path = public
as $$
declare
  v_booking public.bookings;
  v_ride    public.rides;
begin
  if p_status not in ('confirmed', 'cancelled') then
    raise exception 'Status must be confirmed or cancelled' using errcode = '22023';
  end if;

  select * into v_booking from public.bookings where id = p_booking_id;
  if not found then
    raise exception 'Booking not found' using errcode = 'P0002';
  end if;

  -- Lock the ride first, then re-read the booking under that lock, so two
  -- concurrent calls on the same ride serialise here.
  select * into v_ride from public.rides where id = v_booking.ride_id for update;
  select * into v_booking from public.bookings where id = p_booking_id for update;

  if v_ride.driver_id <> auth.uid() then
    raise exception 'Only the ride''s driver can change this booking' using errcode = '42501';
  end if;

  if v_booking.status = p_status then
    return v_booking;  -- Idempotent: no double decrement on a repeated confirm.
  end if;

  if p_status = 'confirmed' then
    if v_ride.available_seats < v_booking.seats then
      raise exception 'Only % seat(s) left on this ride', v_ride.available_seats
        using errcode = '23514';
    end if;
    update public.rides
       set available_seats = available_seats - v_booking.seats
     where id = v_ride.id;
  elsif v_booking.status = 'confirmed' then
    -- Undoing a confirmed booking hands the seats back.
    update public.rides
       set available_seats = available_seats + v_booking.seats
     where id = v_ride.id;
  end if;

  update public.bookings
     set status = p_status
   where id = p_booking_id
  returning * into v_booking;

  return v_booking;
end;
$$;

revoke all on function public.set_booking_status(uuid, text) from public, anon;
grant execute on function public.set_booking_status(uuid, text) to authenticated;
