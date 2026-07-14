--
-- PostgreSQL database dump
--

\restrict NqoZJbzB76FClIR0ZhDa1U94tFe89K4ZrkJcVT11HAt6pzdBXyRDVwZuQtScz4v

-- Dumped from database version 15.18
-- Dumped by pg_dump version 15.18

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.accounts (
    id bigint NOT NULL,
    account_name character varying(255) NOT NULL,
    encrypted_api_key text NOT NULL,
    encrypted_secret_key text NOT NULL,
    risk_percentage numeric(4,2) DEFAULT 0.01 NOT NULL,
    balance numeric(16,2) DEFAULT '0'::numeric NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.accounts OWNER TO myquantuser;

--
-- Name: accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.accounts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.accounts_id_seq OWNER TO myquantuser;

--
-- Name: accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.accounts_id_seq OWNED BY public.accounts.id;


--
-- Name: active_symbols; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.active_symbols (
    id bigint NOT NULL,
    symbol character varying(255) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.active_symbols OWNER TO myquantuser;

--
-- Name: active_symbols_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.active_symbols_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.active_symbols_id_seq OWNER TO myquantuser;

--
-- Name: active_symbols_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.active_symbols_id_seq OWNED BY public.active_symbols.id;


--
-- Name: active_trades; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.active_trades (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    symbol character varying(255) NOT NULL,
    side character varying(255) NOT NULL,
    entry_price numeric(16,8) NOT NULL,
    quantity numeric(16,8) NOT NULL,
    status character varying(255) DEFAULT 'OPEN'::character varying NOT NULL,
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.active_trades OWNER TO myquantuser;

--
-- Name: active_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.active_trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.active_trades_id_seq OWNER TO myquantuser;

--
-- Name: active_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.active_trades_id_seq OWNED BY public.active_trades.id;


--
-- Name: cache; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.cache (
    key character varying(255) NOT NULL,
    value text NOT NULL,
    expiration bigint NOT NULL
);


ALTER TABLE public.cache OWNER TO myquantuser;

--
-- Name: cache_locks; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.cache_locks (
    key character varying(255) NOT NULL,
    owner character varying(255) NOT NULL,
    expiration bigint NOT NULL
);


ALTER TABLE public.cache_locks OWNER TO myquantuser;

--
-- Name: failed_jobs; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.failed_jobs (
    id bigint NOT NULL,
    uuid character varying(255) NOT NULL,
    connection character varying(255) NOT NULL,
    queue character varying(255) NOT NULL,
    payload text NOT NULL,
    exception text NOT NULL,
    failed_at timestamp(0) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


ALTER TABLE public.failed_jobs OWNER TO myquantuser;

--
-- Name: failed_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.failed_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.failed_jobs_id_seq OWNER TO myquantuser;

--
-- Name: failed_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.failed_jobs_id_seq OWNED BY public.failed_jobs.id;


--
-- Name: job_batches; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.job_batches (
    id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    total_jobs integer NOT NULL,
    pending_jobs integer NOT NULL,
    failed_jobs integer NOT NULL,
    failed_job_ids text NOT NULL,
    options text,
    cancelled_at integer,
    created_at integer NOT NULL,
    finished_at integer
);


ALTER TABLE public.job_batches OWNER TO myquantuser;

--
-- Name: jobs; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.jobs (
    id bigint NOT NULL,
    queue character varying(255) NOT NULL,
    payload text NOT NULL,
    attempts smallint NOT NULL,
    reserved_at integer,
    available_at integer NOT NULL,
    created_at integer NOT NULL
);


ALTER TABLE public.jobs OWNER TO myquantuser;

--
-- Name: jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.jobs_id_seq OWNER TO myquantuser;

--
-- Name: jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.jobs_id_seq OWNED BY public.jobs.id;


--
-- Name: macro_state; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.macro_state (
    id integer NOT NULL,
    symbol character varying NOT NULL,
    macro_trend character varying NOT NULL,
    sma_50 double precision NOT NULL,
    z_score double precision NOT NULL,
    std_dev double precision,
    sdc_upper double precision,
    sdc_lower double precision,
    current_price double precision NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.macro_state OWNER TO myquantuser;

--
-- Name: macro_state_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.macro_state_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.macro_state_id_seq OWNER TO myquantuser;

--
-- Name: macro_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.macro_state_id_seq OWNED BY public.macro_state.id;


--
-- Name: migrations; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.migrations (
    id integer NOT NULL,
    migration character varying(255) NOT NULL,
    batch integer NOT NULL
);


ALTER TABLE public.migrations OWNER TO myquantuser;

--
-- Name: migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.migrations_id_seq OWNER TO myquantuser;

--
-- Name: migrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.migrations_id_seq OWNED BY public.migrations.id;


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.password_reset_tokens (
    email character varying(255) NOT NULL,
    token character varying(255) NOT NULL,
    created_at timestamp(0) without time zone
);


ALTER TABLE public.password_reset_tokens OWNER TO myquantuser;

--
-- Name: personal_access_tokens; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.personal_access_tokens (
    id bigint NOT NULL,
    tokenable_type character varying(255) NOT NULL,
    tokenable_id bigint NOT NULL,
    name text NOT NULL,
    token character varying(64) NOT NULL,
    abilities text,
    last_used_at timestamp(0) without time zone,
    expires_at timestamp(0) without time zone,
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.personal_access_tokens OWNER TO myquantuser;

--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.personal_access_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.personal_access_tokens_id_seq OWNER TO myquantuser;

--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.personal_access_tokens_id_seq OWNED BY public.personal_access_tokens.id;


--
-- Name: positions; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.positions (
    id integer NOT NULL,
    symbol character varying(50) NOT NULL,
    asset_balance numeric(18,8) DEFAULT 0,
    pnl_usd numeric(18,2) DEFAULT 0,
    status character varying(20) DEFAULT 'open'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE public.positions OWNER TO myquantuser;

--
-- Name: positions_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.positions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.positions_id_seq OWNER TO myquantuser;

--
-- Name: positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.positions_id_seq OWNED BY public.positions.id;


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.sessions (
    id character varying(255) NOT NULL,
    user_id bigint,
    ip_address character varying(45),
    user_agent text,
    payload text NOT NULL,
    last_activity integer NOT NULL
);


ALTER TABLE public.sessions OWNER TO myquantuser;

--
-- Name: trade_logs; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.trade_logs (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    symbol character varying(255) NOT NULL,
    action_type character varying(255) NOT NULL,
    rationale jsonb NOT NULL,
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.trade_logs OWNER TO myquantuser;

--
-- Name: trade_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.trade_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.trade_logs_id_seq OWNER TO myquantuser;

--
-- Name: trade_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.trade_logs_id_seq OWNED BY public.trade_logs.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: myquantuser
--

CREATE TABLE public.users (
    id bigint NOT NULL,
    name character varying(255) NOT NULL,
    email character varying(255) NOT NULL,
    email_verified_at timestamp(0) without time zone,
    password character varying(255) NOT NULL,
    remember_token character varying(100),
    created_at timestamp(0) without time zone,
    updated_at timestamp(0) without time zone
);


ALTER TABLE public.users OWNER TO myquantuser;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: myquantuser
--

CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.users_id_seq OWNER TO myquantuser;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: myquantuser
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: accounts id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.accounts ALTER COLUMN id SET DEFAULT nextval('public.accounts_id_seq'::regclass);


--
-- Name: active_symbols id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_symbols ALTER COLUMN id SET DEFAULT nextval('public.active_symbols_id_seq'::regclass);


--
-- Name: active_trades id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_trades ALTER COLUMN id SET DEFAULT nextval('public.active_trades_id_seq'::regclass);


--
-- Name: failed_jobs id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.failed_jobs ALTER COLUMN id SET DEFAULT nextval('public.failed_jobs_id_seq'::regclass);


--
-- Name: jobs id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.jobs ALTER COLUMN id SET DEFAULT nextval('public.jobs_id_seq'::regclass);


--
-- Name: macro_state id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.macro_state ALTER COLUMN id SET DEFAULT nextval('public.macro_state_id_seq'::regclass);


--
-- Name: migrations id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.migrations ALTER COLUMN id SET DEFAULT nextval('public.migrations_id_seq'::regclass);


--
-- Name: personal_access_tokens id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.personal_access_tokens ALTER COLUMN id SET DEFAULT nextval('public.personal_access_tokens_id_seq'::regclass);


--
-- Name: positions id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.positions ALTER COLUMN id SET DEFAULT nextval('public.positions_id_seq'::regclass);


--
-- Name: trade_logs id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.trade_logs ALTER COLUMN id SET DEFAULT nextval('public.trade_logs_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: accounts; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.accounts (id, account_name, encrypted_api_key, encrypted_secret_key, risk_percentage, balance, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: active_symbols; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.active_symbols (id, symbol, is_active, created_at, updated_at) FROM stdin;
1	BTCUSDT	t	\N	\N
2	ETHUSDT	t	\N	\N
3	SOLUSDT	t	\N	\N
\.


--
-- Data for Name: active_trades; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.active_trades (id, account_id, symbol, side, entry_price, quantity, status, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: cache; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.cache (key, value, expiration) FROM stdin;
\.


--
-- Data for Name: cache_locks; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.cache_locks (key, owner, expiration) FROM stdin;
\.


--
-- Data for Name: failed_jobs; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.failed_jobs (id, uuid, connection, queue, payload, exception, failed_at) FROM stdin;
\.


--
-- Data for Name: job_batches; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.job_batches (id, name, total_jobs, pending_jobs, failed_jobs, failed_job_ids, options, cancelled_at, created_at, finished_at) FROM stdin;
\.


--
-- Data for Name: jobs; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.jobs (id, queue, payload, attempts, reserved_at, available_at, created_at) FROM stdin;
\.


--
-- Data for Name: macro_state; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.macro_state (id, symbol, macro_trend, sma_50, z_score, std_dev, sdc_upper, sdc_lower, current_price, updated_at) FROM stdin;
1	BTCUSDT	DOWNTREND	63833.424000000006	-1.7941756152472215	502.02666469519346	64837.47732939039	62829.37067060962	62932.7	2026-07-13 11:47:35.410759
2	ETHUSDT	DOWNTREND	1804.6584	-1.5292551108593755	14.803547059769668	1834.2654941195394	1775.0513058804606	1782.02	2026-07-13 11:47:35.455967
\.


--
-- Data for Name: migrations; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.migrations (id, migration, batch) FROM stdin;
1	0001_01_01_000000_create_users_table	1
2	0001_01_01_000001_create_cache_table	1
3	0001_01_01_000002_create_jobs_table	1
4	2026_06_25_192631_create_accounts_table	1
5	2026_06_25_192631_create_active_trades_table	1
6	2026_06_25_192632_create_trade_logs_table	1
7	2026_06_25_204233_create_personal_access_tokens_table	1
8	2026_07_09_212150_create_active_symbols_table	2
\.


--
-- Data for Name: password_reset_tokens; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.password_reset_tokens (email, token, created_at) FROM stdin;
\.


--
-- Data for Name: personal_access_tokens; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.personal_access_tokens (id, tokenable_type, tokenable_id, name, token, abilities, last_used_at, expires_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: positions; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.positions (id, symbol, asset_balance, pnl_usd, status, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: sessions; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.sessions (id, user_id, ip_address, user_agent, payload, last_activity) FROM stdin;
u59pRV4O51yN6CzMXKuQJ2MR5tE6TRiU6seiL6qk	\N	172.18.0.1	Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36	eyJfdG9rZW4iOiJLekxJU3dHOGQwRFN4V21rbXBOUE5vSjlKeEtxRnJUNkxOckZ1anNJIiwiX3ByZXZpb3VzIjp7InVybCI6Imh0dHA6XC9cLzEyNy4wLjAuMTo4MDAwIiwicm91dGUiOm51bGx9LCJfZmxhc2giOnsib2xkIjpbXSwibmV3IjpbXX19	1783637653
\.


--
-- Data for Name: trade_logs; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.trade_logs (id, account_id, symbol, action_type, rationale, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: myquantuser
--

COPY public.users (id, name, email, email_verified_at, password, remember_token, created_at, updated_at) FROM stdin;
\.


--
-- Name: accounts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.accounts_id_seq', 1, false);


--
-- Name: active_symbols_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.active_symbols_id_seq', 3, true);


--
-- Name: active_trades_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.active_trades_id_seq', 1, false);


--
-- Name: failed_jobs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.failed_jobs_id_seq', 1, false);


--
-- Name: jobs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.jobs_id_seq', 1, false);


--
-- Name: macro_state_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.macro_state_id_seq', 2, true);


--
-- Name: migrations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.migrations_id_seq', 8, true);


--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.personal_access_tokens_id_seq', 1, false);


--
-- Name: positions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.positions_id_seq', 1, false);


--
-- Name: trade_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.trade_logs_id_seq', 1, false);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: myquantuser
--

SELECT pg_catalog.setval('public.users_id_seq', 1, false);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: active_symbols active_symbols_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_symbols
    ADD CONSTRAINT active_symbols_pkey PRIMARY KEY (id);


--
-- Name: active_symbols active_symbols_symbol_unique; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_symbols
    ADD CONSTRAINT active_symbols_symbol_unique UNIQUE (symbol);


--
-- Name: active_trades active_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_trades
    ADD CONSTRAINT active_trades_pkey PRIMARY KEY (id);


--
-- Name: cache_locks cache_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.cache_locks
    ADD CONSTRAINT cache_locks_pkey PRIMARY KEY (key);


--
-- Name: cache cache_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.cache
    ADD CONSTRAINT cache_pkey PRIMARY KEY (key);


--
-- Name: failed_jobs failed_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_pkey PRIMARY KEY (id);


--
-- Name: failed_jobs failed_jobs_uuid_unique; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_uuid_unique UNIQUE (uuid);


--
-- Name: job_batches job_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.job_batches
    ADD CONSTRAINT job_batches_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: macro_state macro_state_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.macro_state
    ADD CONSTRAINT macro_state_pkey PRIMARY KEY (id);


--
-- Name: migrations migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (email);


--
-- Name: personal_access_tokens personal_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_pkey PRIMARY KEY (id);


--
-- Name: personal_access_tokens personal_access_tokens_token_unique; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_token_unique UNIQUE (token);


--
-- Name: positions positions_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: trade_logs trade_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.trade_logs
    ADD CONSTRAINT trade_logs_pkey PRIMARY KEY (id);


--
-- Name: users users_email_unique; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_unique UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: active_trades_symbol_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX active_trades_symbol_index ON public.active_trades USING btree (symbol);


--
-- Name: cache_expiration_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX cache_expiration_index ON public.cache USING btree (expiration);


--
-- Name: cache_locks_expiration_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX cache_locks_expiration_index ON public.cache_locks USING btree (expiration);


--
-- Name: failed_jobs_connection_queue_failed_at_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX failed_jobs_connection_queue_failed_at_index ON public.failed_jobs USING btree (connection, queue, failed_at);


--
-- Name: ix_macro_state_id; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX ix_macro_state_id ON public.macro_state USING btree (id);


--
-- Name: ix_macro_state_symbol; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE UNIQUE INDEX ix_macro_state_symbol ON public.macro_state USING btree (symbol);


--
-- Name: jobs_queue_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX jobs_queue_index ON public.jobs USING btree (queue);


--
-- Name: personal_access_tokens_expires_at_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX personal_access_tokens_expires_at_index ON public.personal_access_tokens USING btree (expires_at);


--
-- Name: personal_access_tokens_tokenable_type_tokenable_id_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX personal_access_tokens_tokenable_type_tokenable_id_index ON public.personal_access_tokens USING btree (tokenable_type, tokenable_id);


--
-- Name: sessions_last_activity_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX sessions_last_activity_index ON public.sessions USING btree (last_activity);


--
-- Name: sessions_user_id_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX sessions_user_id_index ON public.sessions USING btree (user_id);


--
-- Name: trade_logs_action_type_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX trade_logs_action_type_index ON public.trade_logs USING btree (action_type);


--
-- Name: trade_logs_symbol_index; Type: INDEX; Schema: public; Owner: myquantuser
--

CREATE INDEX trade_logs_symbol_index ON public.trade_logs USING btree (symbol);


--
-- Name: active_trades active_trades_account_id_foreign; Type: FK CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.active_trades
    ADD CONSTRAINT active_trades_account_id_foreign FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--
-- Name: trade_logs trade_logs_account_id_foreign; Type: FK CONSTRAINT; Schema: public; Owner: myquantuser
--

ALTER TABLE ONLY public.trade_logs
    ADD CONSTRAINT trade_logs_account_id_foreign FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict NqoZJbzB76FClIR0ZhDa1U94tFe89K4ZrkJcVT11HAt6pzdBXyRDVwZuQtScz4v

