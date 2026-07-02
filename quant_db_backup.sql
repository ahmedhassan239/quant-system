--
-- PostgreSQL database dump
--

\restrict IM9ovln4RdCawJaAHUZuyFAQc7vnHqL3bUVECyW83uDuMd1MaZ8tmPryIXfUeeM

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
-- Name: accounts; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.accounts OWNER TO quant_user;

--
-- Name: accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.accounts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.accounts_id_seq OWNER TO quant_user;

--
-- Name: accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.accounts_id_seq OWNED BY public.accounts.id;


--
-- Name: active_trades; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.active_trades OWNER TO quant_user;

--
-- Name: active_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.active_trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.active_trades_id_seq OWNER TO quant_user;

--
-- Name: active_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.active_trades_id_seq OWNED BY public.active_trades.id;


--
-- Name: cache; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.cache (
    key character varying(255) NOT NULL,
    value text NOT NULL,
    expiration bigint NOT NULL
);


ALTER TABLE public.cache OWNER TO quant_user;

--
-- Name: cache_locks; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.cache_locks (
    key character varying(255) NOT NULL,
    owner character varying(255) NOT NULL,
    expiration bigint NOT NULL
);


ALTER TABLE public.cache_locks OWNER TO quant_user;

--
-- Name: failed_jobs; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.failed_jobs OWNER TO quant_user;

--
-- Name: failed_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.failed_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.failed_jobs_id_seq OWNER TO quant_user;

--
-- Name: failed_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.failed_jobs_id_seq OWNED BY public.failed_jobs.id;


--
-- Name: job_batches; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.job_batches OWNER TO quant_user;

--
-- Name: jobs; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.jobs OWNER TO quant_user;

--
-- Name: jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.jobs_id_seq OWNER TO quant_user;

--
-- Name: jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.jobs_id_seq OWNED BY public.jobs.id;


--
-- Name: market_data; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.market_data (
    id integer NOT NULL,
    symbol character varying NOT NULL,
    timeframe character varying NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision NOT NULL
);


ALTER TABLE public.market_data OWNER TO quant_user;

--
-- Name: market_data_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.market_data_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.market_data_id_seq OWNER TO quant_user;

--
-- Name: market_data_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.market_data_id_seq OWNED BY public.market_data.id;


--
-- Name: migrations; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.migrations (
    id integer NOT NULL,
    migration character varying(255) NOT NULL,
    batch integer NOT NULL
);


ALTER TABLE public.migrations OWNER TO quant_user;

--
-- Name: migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.migrations_id_seq OWNER TO quant_user;

--
-- Name: migrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.migrations_id_seq OWNED BY public.migrations.id;


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.password_reset_tokens (
    email character varying(255) NOT NULL,
    token character varying(255) NOT NULL,
    created_at timestamp(0) without time zone
);


ALTER TABLE public.password_reset_tokens OWNER TO quant_user;

--
-- Name: personal_access_tokens; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.personal_access_tokens OWNER TO quant_user;

--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.personal_access_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.personal_access_tokens_id_seq OWNER TO quant_user;

--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.personal_access_tokens_id_seq OWNED BY public.personal_access_tokens.id;


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.sessions (
    id character varying(255) NOT NULL,
    user_id bigint,
    ip_address character varying(45),
    user_agent text,
    payload text NOT NULL,
    last_activity integer NOT NULL
);


ALTER TABLE public.sessions OWNER TO quant_user;

--
-- Name: trade_logs; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.trade_logs OWNER TO quant_user;

--
-- Name: trade_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.trade_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.trade_logs_id_seq OWNER TO quant_user;

--
-- Name: trade_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.trade_logs_id_seq OWNED BY public.trade_logs.id;


--
-- Name: trading_signals; Type: TABLE; Schema: public; Owner: quant_user
--

CREATE TABLE public.trading_signals (
    id integer NOT NULL,
    symbol character varying NOT NULL,
    timeframe character varying NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    current_price double precision NOT NULL,
    rsi double precision,
    bullish_ob_low double precision,
    bullish_ob_high double precision,
    bearish_ob_low double precision,
    bearish_ob_high double precision,
    decision character varying NOT NULL
);


ALTER TABLE public.trading_signals OWNER TO quant_user;

--
-- Name: trading_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.trading_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.trading_signals_id_seq OWNER TO quant_user;

--
-- Name: trading_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.trading_signals_id_seq OWNED BY public.trading_signals.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: quant_user
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


ALTER TABLE public.users OWNER TO quant_user;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: quant_user
--

CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.users_id_seq OWNER TO quant_user;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quant_user
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: accounts id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.accounts ALTER COLUMN id SET DEFAULT nextval('public.accounts_id_seq'::regclass);


--
-- Name: active_trades id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.active_trades ALTER COLUMN id SET DEFAULT nextval('public.active_trades_id_seq'::regclass);


--
-- Name: failed_jobs id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.failed_jobs ALTER COLUMN id SET DEFAULT nextval('public.failed_jobs_id_seq'::regclass);


--
-- Name: jobs id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.jobs ALTER COLUMN id SET DEFAULT nextval('public.jobs_id_seq'::regclass);


--
-- Name: market_data id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.market_data ALTER COLUMN id SET DEFAULT nextval('public.market_data_id_seq'::regclass);


--
-- Name: migrations id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.migrations ALTER COLUMN id SET DEFAULT nextval('public.migrations_id_seq'::regclass);


--
-- Name: personal_access_tokens id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.personal_access_tokens ALTER COLUMN id SET DEFAULT nextval('public.personal_access_tokens_id_seq'::regclass);


--
-- Name: trade_logs id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.trade_logs ALTER COLUMN id SET DEFAULT nextval('public.trade_logs_id_seq'::regclass);


--
-- Name: trading_signals id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.trading_signals ALTER COLUMN id SET DEFAULT nextval('public.trading_signals_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: accounts; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.accounts (id, account_name, encrypted_api_key, encrypted_secret_key, risk_percentage, balance, is_active, created_at, updated_at) FROM stdin;
1	Test_Account_1	dummy_api_key	dummy_secret	0.01	1000.00	t	2026-06-25 19:55:56	2026-06-25 19:55:56
\.


--
-- Data for Name: active_trades; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.active_trades (id, account_id, symbol, side, entry_price, quantity, status, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: cache; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.cache (key, value, expiration) FROM stdin;
\.


--
-- Data for Name: cache_locks; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.cache_locks (key, owner, expiration) FROM stdin;
\.


--
-- Data for Name: failed_jobs; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.failed_jobs (id, uuid, connection, queue, payload, exception, failed_at) FROM stdin;
\.


--
-- Data for Name: job_batches; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.job_batches (id, name, total_jobs, pending_jobs, failed_jobs, failed_job_ids, options, cancelled_at, created_at, finished_at) FROM stdin;
\.


--
-- Data for Name: jobs; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.jobs (id, queue, payload, attempts, reserved_at, available_at, created_at) FROM stdin;
\.


--
-- Data for Name: market_data; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.market_data (id, symbol, timeframe, "timestamp", open, high, low, close, volume) FROM stdin;
1	BTCUSDT	15m	2026-06-25 17:00:00	59312.64	59464	59139.96	59399.77	293.76464
2	BTCUSDT	15m	2026-06-25 17:15:00	59399.77	59602	59342.4	59556.01	267.29296
3	BTCUSDT	15m	2026-06-25 17:30:00	59556	59740.9	59496.49	59535.81	239.74113
4	BTCUSDT	15m	2026-06-25 17:45:00	59535.81	59684	59443.8	59676.01	262.07761
5	BTCUSDT	15m	2026-06-25 18:00:00	59676	59793.34	59400	59400.01	315.34678
6	BTCUSDT	15m	2026-06-25 18:15:00	59400.01	59749.99	59398.01	59718.02	277.36995
7	BTCUSDT	15m	2026-06-25 18:30:00	59718.01	59718.02	59382.43	59423.88	205.9677
8	BTCUSDT	15m	2026-06-25 18:45:00	59423.88	59594.77	59364	59512.71	180.06658
9	BTCUSDT	15m	2026-06-25 19:00:00	59512.71	59516	59227.15	59242.01	240.53845
10	BTCUSDT	15m	2026-06-25 19:15:00	59242.01	59359.19	59168.82	59348.05	275.50858
11	BTCUSDT	15m	2026-06-25 19:30:00	59348.04	59434.05	59289.61	59301.99	214.87059
12	BTCUSDT	15m	2026-06-25 19:45:00	59301.99	59365.84	59188.25	59320	186.37131
13	BTCUSDT	15m	2026-06-25 20:00:00	59319.99	59741.95	59319.99	59580	239.20727
14	BTCUSDT	15m	2026-06-25 20:15:00	59580	59730	59553.39	59628	99.49196
15	BTCUSDT	15m	2026-06-25 20:30:00	59628	59660	59414.04	59522.01	108.15795
16	BTCUSDT	15m	2026-06-25 20:45:00	59522.01	59522.01	59344	59456.62	92.5438
17	BTCUSDT	15m	2026-06-25 21:00:00	59456.62	59622.09	59452.01	59622.09	111.97121
18	BTCUSDT	15m	2026-06-25 21:15:00	59622.09	59711.99	59557.6	59711.99	91.66054
19	BTCUSDT	15m	2026-06-25 21:30:00	59712	60005	59700.01	59958.01	198.95353
20	BTCUSDT	15m	2026-06-25 21:45:00	59958	60273.81	59898	60211.87	214.63746
21	BTCUSDT	15m	2026-06-25 22:00:00	60211.86	60211.86	59879.66	59887.06	174.58293
22	BTCUSDT	15m	2026-06-25 22:15:00	59887.05	60012	59794	59794.01	95.74875
23	BTCUSDT	15m	2026-06-25 22:30:00	59794.01	59874.02	59750	59794	75.64434
24	BTCUSDT	15m	2026-06-25 22:45:00	59793.99	59921.71	59740	59851.27	85.63374
25	BTCUSDT	15m	2026-06-25 23:00:00	59851.27	59967.44	59796.02	59946	72.10612
26	BTCUSDT	15m	2026-06-25 23:15:00	59946	60170.17	59926	60104.51	222.09557
27	BTCUSDT	15m	2026-06-25 23:30:00	60104.51	60191.55	59965.99	59986	90.07977
28	BTCUSDT	15m	2026-06-25 23:45:00	59986	59986.01	59739.99	59794	74.61217
29	BTCUSDT	15m	2026-06-26 00:00:00	59794.64	59906.71	59771.13	59884.01	192.32459
30	BTCUSDT	15m	2026-06-26 00:15:00	59884	59884	59679.63	59771.99	217.56002
31	BTCUSDT	15m	2026-06-26 00:30:00	59771.99	59871.99	59704	59789.8	109.91069
32	BTCUSDT	15m	2026-06-26 00:45:00	59789.79	59818	59642.77	59667.09	355.81997
33	BTCUSDT	15m	2026-06-26 01:00:00	59667.09	59789.07	59450	59595.99	261.17661
34	BTCUSDT	15m	2026-06-26 01:15:00	59596	59664	59300.09	59300.09	239.24443
35	BTCUSDT	15m	2026-06-26 01:30:00	59300.1	59379.34	59228	59313.93	202.73273
36	BTCUSDT	15m	2026-06-26 01:45:00	59313.92	59439.31	59271.18	59431.5	130.55916
37	BTCUSDT	15m	2026-06-26 02:00:00	59431.49	59456	58608.2	58678	767.40607
38	BTCUSDT	15m	2026-06-26 02:15:00	58678	58892.21	58358.77	58882.01	959.88104
39	BTCUSDT	15m	2026-06-26 02:30:00	58882.01	59012.48	58690.69	58817.88	546.39283
40	BTCUSDT	15m	2026-06-26 02:45:00	58817.88	58828.48	58435.32	58561.99	360.00356
41	BTCUSDT	15m	2026-06-26 03:00:00	58562	58970	58337	58898.01	470.8781
42	BTCUSDT	15m	2026-06-26 03:15:00	58898	59186.53	58804	59100.36	257.97616
43	BTCUSDT	15m	2026-06-26 03:30:00	59100.37	59539.49	59014.9	59494.16	420.2586
44	BTCUSDT	15m	2026-06-26 03:45:00	59494.16	60131.37	59484.21	60036.01	736.95949
45	BTCUSDT	15m	2026-06-26 04:00:00	60036	60091.99	59836.17	59914.73	347.18572
46	BTCUSDT	15m	2026-06-26 04:15:00	59914.73	60238.77	59891.49	59994.01	248.9507
47	BTCUSDT	15m	2026-06-26 04:30:00	59994	60020.31	59891.78	59973.73	138.99965
48	BTCUSDT	15m	2026-06-26 04:45:00	59973.73	59979.35	59836.77	59920.47	108.84233
49	BTCUSDT	15m	2026-06-26 05:00:00	59920.47	59976	59838.01	59976	200.19302
50	BTCUSDT	15m	2026-06-26 05:15:00	59976	60040	59736	59958	218.44645
51	BTCUSDT	15m	2026-06-26 05:30:00	59958.01	60047.68	59847.05	59881.74	136.41831
52	BTCUSDT	15m	2026-06-26 05:45:00	59881.74	59973.6	59780.04	59953.3	328.07871
53	BTCUSDT	15m	2026-06-26 06:00:00	59953.3	59953.31	59702	59719.27	622.79632
54	BTCUSDT	15m	2026-06-26 06:15:00	59719.27	60093.38	59715.76	60045.49	313.95605
55	BTCUSDT	15m	2026-06-26 06:30:00	60045.48	60414	59954.31	60390.93	321.80887
56	BTCUSDT	15m	2026-06-26 06:45:00	60390.92	60459.55	60240	60324	246.41803
57	BTCUSDT	15m	2026-06-26 07:00:00	60324	60546	60283.08	60514.88	183.12083
58	BTCUSDT	15m	2026-06-26 07:15:00	60514.89	60713.26	60470	60590.01	286.76485
59	BTCUSDT	15m	2026-06-26 07:30:00	60590.01	60759.99	60559.57	60594.01	798.7045
60	BTCUSDT	15m	2026-06-26 07:45:00	60594	60642.92	60500	60532	884.48061
61	BTCUSDT	15m	2026-06-26 08:00:00	60532	60580	60233.31	60276.04	292.08296
62	BTCUSDT	15m	2026-06-26 08:15:00	60276.17	60375.47	60191.04	60314.5	207.15626
63	BTCUSDT	15m	2026-06-26 08:30:00	60314.51	60348.98	60153.75	60214	165.94764
64	BTCUSDT	15m	2026-06-26 08:45:00	60214	60280	60150.56	60245.67	138.66778
65	BTCUSDT	15m	2026-06-26 09:00:00	60245.9	60259.37	60080.18	60175.68	118.5071
66	BTCUSDT	15m	2026-06-26 09:15:00	60175.68	60179.36	60084.01	60134	142.29569
67	BTCUSDT	15m	2026-06-26 09:30:00	60133.99	60133.99	59957.04	60003.51	212.53875
68	BTCUSDT	15m	2026-06-26 09:45:00	60003.51	60024.43	59583.33	59736.01	358.88048
69	BTCUSDT	15m	2026-06-26 10:00:00	59736.01	59825.09	59656	59752	252.58312
70	BTCUSDT	15m	2026-06-26 10:15:00	59752.01	59858.93	59718.78	59723.7	237.13806
71	BTCUSDT	15m	2026-06-26 10:30:00	59723.7	59809	59459.83	59558.01	210.63817
72	BTCUSDT	15m	2026-06-26 10:45:00	59558.01	59566	59239.78	59313.26	350.61506
73	BTCUSDT	15m	2026-06-26 11:00:00	59313.26	59588	59259.78	59486	382.26928
74	BTCUSDT	15m	2026-06-26 11:15:00	59486	59500	59368.85	59419.01	136.62719
75	BTCUSDT	15m	2026-06-26 11:30:00	59419.02	59532	59390.12	59452.58	174.80058
76	BTCUSDT	15m	2026-06-26 11:45:00	59452.58	59453.07	59275.41	59413.24	354.63585
77	BTCUSDT	15m	2026-06-26 12:00:00	59413.24	59517.52	59343.44	59455.39	409.13623
78	BTCUSDT	15m	2026-06-26 12:15:00	59455.4	59726	59453.39	59726	354.8596
79	BTCUSDT	15m	2026-06-26 12:30:00	59725.99	60007.91	58969.9	59145.33	499.05607
80	BTCUSDT	15m	2026-06-26 12:45:00	59145.33	59600	59072.73	59185.02	447.30191
81	BTCUSDT	15m	2026-06-26 13:00:00	59185.02	59279.84	58947.16	59093.9	361.84089
82	BTCUSDT	15m	2026-06-26 13:15:00	59093.9	59174.84	58500.1	58914.35	1118.33001
83	BTCUSDT	15m	2026-06-26 13:30:00	58914.35	59508.2	58842	59480.49	648.79123
84	BTCUSDT	15m	2026-06-26 13:45:00	59480.48	60216.14	59328.79	60175.43	601.37666
85	BTCUSDT	15m	2026-06-26 14:00:00	60175.42	60294	59815.34	59989.99	555.75868
86	BTCUSDT	15m	2026-06-26 14:15:00	59989.99	60253.04	59821.24	60059.34	416.32035
87	BTCUSDT	15m	2026-06-26 14:30:00	60059.35	60080.36	59556	59600	456.95276
88	BTCUSDT	15m	2026-06-26 14:45:00	59600	59906	59390	59590.73	316.24688
89	BTCUSDT	15m	2026-06-26 15:00:00	59590.73	60100	59584.01	59834.01	309.23207
90	BTCUSDT	15m	2026-06-26 15:15:00	59834	60376	59786	60185.99	418.57807
91	BTCUSDT	15m	2026-06-26 15:30:00	60185.98	60500	60129.99	60424.13	458.18767
92	BTCUSDT	15m	2026-06-26 15:45:00	60424.13	60438	60144.82	60328.32	396.87307
93	BTCUSDT	15m	2026-06-26 16:00:00	60328.18	60583	60147.36	60178	418.74818
94	BTCUSDT	15m	2026-06-26 16:15:00	60178	60278	59970	60042.99	264.48641
95	BTCUSDT	15m	2026-06-26 16:30:00	60042.99	60387.99	60042.99	60066.13	292.90203
96	BTCUSDT	15m	2026-06-26 16:45:00	60066.12	60181.68	60014	60041.75	152.33255
97	BTCUSDT	15m	2026-06-26 17:00:00	60041.76	60287.9	60011.04	60211.99	166.24192
98	BTCUSDT	15m	2026-06-26 17:15:00	60211.99	60254.2	60036.79	60178.01	138.9132
99	BTCUSDT	15m	2026-06-26 17:30:00	60178	60216	59952	60065.5	121.85771
100	BTCUSDT	15m	2026-06-26 17:45:00	60065.5	60108	59972	60015.99	66.99504
\.


--
-- Data for Name: migrations; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.migrations (id, migration, batch) FROM stdin;
1	0001_01_01_000000_create_users_table	1
2	0001_01_01_000001_create_cache_table	1
3	0001_01_01_000002_create_jobs_table	1
4	2026_06_25_192631_create_accounts_table	1
5	2026_06_25_192631_create_active_trades_table	1
6	2026_06_25_192632_create_trade_logs_table	1
7	2026_06_25_204233_create_personal_access_tokens_table	2
\.


--
-- Data for Name: password_reset_tokens; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.password_reset_tokens (email, token, created_at) FROM stdin;
\.


--
-- Data for Name: personal_access_tokens; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.personal_access_tokens (id, tokenable_type, tokenable_id, name, token, abilities, last_used_at, expires_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: sessions; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.sessions (id, user_id, ip_address, user_agent, payload, last_activity) FROM stdin;
\.


--
-- Data for Name: trade_logs; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.trade_logs (id, account_id, symbol, action_type, rationale, created_at, updated_at) FROM stdin;
1	1	SOLUSDT	EXECUTE_MARKET_BUY	{"order_flow": {"delta_volume_spike_x": 3.4, "limit_order_absorption": "HIGH_BUY_PRESSURE", "whale_wall_detected_at": 149.5}, "market_structure": {"status": "BULLISH_SHIFT", "timeframe": "5m", "detected_pattern": "Fair Value Gap (FVG) Reversal", "liquidity_pool_hit": true}, "news_intelligence": {"headline": "Network upgrade consensus achieved with major validators support", "ai_model_used": "FinBERT", "classification": "STRONG_BULLISH", "source_scraped": "Binance Announcements / Bloomberg", "sentiment_score": 0.86}}	2026-06-25 19:55:56	2026-06-25 19:55:56
\.


--
-- Data for Name: trading_signals; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.trading_signals (id, symbol, timeframe, "timestamp", current_price, rsi, bullish_ob_low, bullish_ob_high, bearish_ob_low, bearish_ob_high, decision) FROM stdin;
1	BTCUSDT	15m	2026-06-26 17:45:00	60015.99	52.004034270563515	59390	59906	60129.99	60500	WAIT
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: quant_user
--

COPY public.users (id, name, email, email_verified_at, password, remember_token, created_at, updated_at) FROM stdin;
\.


--
-- Name: accounts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.accounts_id_seq', 1, true);


--
-- Name: active_trades_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.active_trades_id_seq', 1, false);


--
-- Name: failed_jobs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.failed_jobs_id_seq', 1, false);


--
-- Name: jobs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.jobs_id_seq', 1, false);


--
-- Name: market_data_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.market_data_id_seq', 100, true);


--
-- Name: migrations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.migrations_id_seq', 7, true);


--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.personal_access_tokens_id_seq', 1, false);


--
-- Name: trade_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.trade_logs_id_seq', 1, true);


--
-- Name: trading_signals_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.trading_signals_id_seq', 1, true);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quant_user
--

SELECT pg_catalog.setval('public.users_id_seq', 1, false);


--
-- Name: market_data _symbol_timeframe_timestamp_uc; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.market_data
    ADD CONSTRAINT _symbol_timeframe_timestamp_uc UNIQUE (symbol, timeframe, "timestamp");


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: active_trades active_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.active_trades
    ADD CONSTRAINT active_trades_pkey PRIMARY KEY (id);


--
-- Name: cache_locks cache_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.cache_locks
    ADD CONSTRAINT cache_locks_pkey PRIMARY KEY (key);


--
-- Name: cache cache_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.cache
    ADD CONSTRAINT cache_pkey PRIMARY KEY (key);


--
-- Name: failed_jobs failed_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_pkey PRIMARY KEY (id);


--
-- Name: failed_jobs failed_jobs_uuid_unique; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_uuid_unique UNIQUE (uuid);


--
-- Name: job_batches job_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.job_batches
    ADD CONSTRAINT job_batches_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: market_data market_data_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.market_data
    ADD CONSTRAINT market_data_pkey PRIMARY KEY (id);


--
-- Name: migrations migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (email);


--
-- Name: personal_access_tokens personal_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_pkey PRIMARY KEY (id);


--
-- Name: personal_access_tokens personal_access_tokens_token_unique; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_token_unique UNIQUE (token);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: trade_logs trade_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.trade_logs
    ADD CONSTRAINT trade_logs_pkey PRIMARY KEY (id);


--
-- Name: trading_signals trading_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.trading_signals
    ADD CONSTRAINT trading_signals_pkey PRIMARY KEY (id);


--
-- Name: users users_email_unique; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_unique UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: active_trades_symbol_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX active_trades_symbol_index ON public.active_trades USING btree (symbol);


--
-- Name: cache_expiration_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX cache_expiration_index ON public.cache USING btree (expiration);


--
-- Name: cache_locks_expiration_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX cache_locks_expiration_index ON public.cache_locks USING btree (expiration);


--
-- Name: failed_jobs_connection_queue_failed_at_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX failed_jobs_connection_queue_failed_at_index ON public.failed_jobs USING btree (connection, queue, failed_at);


--
-- Name: ix_market_data_id; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX ix_market_data_id ON public.market_data USING btree (id);


--
-- Name: ix_trading_signals_id; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX ix_trading_signals_id ON public.trading_signals USING btree (id);


--
-- Name: jobs_queue_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX jobs_queue_index ON public.jobs USING btree (queue);


--
-- Name: personal_access_tokens_expires_at_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX personal_access_tokens_expires_at_index ON public.personal_access_tokens USING btree (expires_at);


--
-- Name: personal_access_tokens_tokenable_type_tokenable_id_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX personal_access_tokens_tokenable_type_tokenable_id_index ON public.personal_access_tokens USING btree (tokenable_type, tokenable_id);


--
-- Name: sessions_last_activity_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX sessions_last_activity_index ON public.sessions USING btree (last_activity);


--
-- Name: sessions_user_id_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX sessions_user_id_index ON public.sessions USING btree (user_id);


--
-- Name: trade_logs_action_type_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX trade_logs_action_type_index ON public.trade_logs USING btree (action_type);


--
-- Name: trade_logs_symbol_index; Type: INDEX; Schema: public; Owner: quant_user
--

CREATE INDEX trade_logs_symbol_index ON public.trade_logs USING btree (symbol);


--
-- Name: active_trades active_trades_account_id_foreign; Type: FK CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.active_trades
    ADD CONSTRAINT active_trades_account_id_foreign FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--
-- Name: trade_logs trade_logs_account_id_foreign; Type: FK CONSTRAINT; Schema: public; Owner: quant_user
--

ALTER TABLE ONLY public.trade_logs
    ADD CONSTRAINT trade_logs_account_id_foreign FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict IM9ovln4RdCawJaAHUZuyFAQc7vnHqL3bUVECyW83uDuMd1MaZ8tmPryIXfUeeM

