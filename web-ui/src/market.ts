// A股代码 → 市场智能识别。
// 用户只输入 6 位代码，按代码段规则自动匹配 沪市(SH)/深市(SZ)/北交所(BJ)，
// 拼成后端要求的 "市场:代码" 格式（如 SZ:000001）。
// 美股/港股等扩展市场用字母代码（如 SPY/QQQ），走 /ex/bars 接口。

export type Market = 'SH' | 'SZ' | 'BJ'

/** 扩展市场标识，对应后端 ExMarket 枚举名。 */
export type ExMarket = 'US_STOCK' | 'HK_MAIN_BOARD'

/**
 * 判断代码是否为扩展市场（美股/港股）。
 * 美股代码为 1-5 位字母（如 SPY/QQQ/AAPL），港股代码为 5 位数字。
 * A 股代码为 6 位数字。
 */
export function isExMarketCode(code: string): boolean {
  const c = code.trim()
  // 美股：纯字母，1-5 位
  if (/^[A-Za-z]{1,5}$/.test(c)) return true
  // 港股：5 位纯数字（与 A 股 6 位区分）
  if (/^\d{5}$/.test(c)) return true
  return false
}

/**
 * 检测扩展市场类型（仅当 isExMarketCode 返回 true 时调用）。
 */
export function detectExMarket(code: string): ExMarket {
  const c = code.trim()
  if (/^[A-Za-z]{1,5}$/.test(c)) return 'US_STOCK'
  return 'HK_MAIN_BOARD'
}

/**
 * 根据 6 位股票代码智能判断所属市场。
 *
 * 规则（按优先级，先匹配到的为准）：
 *   - 北交所(BJ)：43/83/87/92/93/920（小盘/三板）或 4xx/8xx 开头
 *   - 沪市(SH) ：6/9 开头（主板 60/68 科创、B 股 900）或 5 开头（基金 50/51/56/58）
 *   - 其余归深市(SZ)：000/001/002/003/300/301 创业板、200 B股 等
 *
 * @param code 6 位股票代码（纯数字）
 * @returns 市场代码 SH/SZ/BJ；无法判断时默认深市（覆盖面最广）
 */
export function detectMarket(code: string): Market {
  const c = code.trim()
  if (!/^\d{6}$/.test(c)) return 'SZ'

  // 北交所：43/83/87/92(含920段)/93 + 4xx/8xx（三板/小盘）
  if (/^(43|83|87|92|93|4|8)/.test(c)) return 'BJ'

  // 沪市：6xx（主板/科创板 60/68）、9xx（B股）、5xx（沪市基金 50/51/56/58/50ETF 等）
  if (/^[695]/.test(c)) return 'SH'

  // 其余归深市：000/001/002/003/300/301/200 等
  return 'SZ'
}

/**
 * 把 6 位代码转成后端要求的 "市场:代码" 格式。
 * @param code 6 位股票代码
 */
export function toSymbol(code: string): string {
  return `${detectMarket(code)}:${code.trim()}`
}

/** 市场中文显示名。 */
export function marketLabel(market: Market | ExMarket): string {
  switch (market) {
    case 'SH':
      return '沪市'
    case 'BJ':
      return '北交所'
    case 'US_STOCK':
      return '美股'
    case 'HK_MAIN_BOARD':
      return '港股'
    default:
      return '深市'
  }
}

/**
 * 常用美股/港股代码中文名映射（扩展市场行情接口不返回名称，
 * 用本地映射补全常见标的；未命中的返回代码本身）。
 */
const EX_NAME_MAP: Record<string, string> = {
  // 美股 ETF
  SPY: '标普500 ETF',
  QQQ: '纳指100 ETF',
  DIA: '道指 ETF',
  IWM: '罗素2000 ETF',
  VTI: '全市场 ETF',
  VOO: '标普500 ETF',
  XLK: '科技 ETF',
  XLF: '金融 ETF',
  XLV: '医疗 ETF',
  XLE: '能源 ETF',
  XLY: '可选消费 ETF',
  XLP: '必需消费 ETF',
  XLI: '工业 ETF',
  XLU: '公用事业 ETF',
  XLB: '材料 ETF',
  XLC: '通信 ETF',
  XRE: '地产 ETF',
  TLT: '20+年美债 ETF',
  GLD: '黄金 ETF',
  SLV: '白银 ETF',
  USO: '原油 ETF',
  // 美股个股（热门）
  AAPL: '苹果',
  MSFT: '微软',
  GOOGL: '谷歌A',
  GOOG: '谷歌C',
  AMZN: '亚马逊',
  META: 'Meta',
  NVDA: '英伟达',
  TSLA: '特斯拉',
  BRK: '伯克希尔',
  JPM: '摩根大通',
  V: 'Visa',
  JNJ: '强生',
  WMT: '沃尔玛',
  MA: '万事达',
  PG: '宝洁',
  UNH: '联合健康',
  HD: '家得宝',
  DIS: '迪士尼',
  NFLX: '奈飞',
  INTC: '英特尔',
  AMD: 'AMD',
  CRM: 'Salesforce',
  ADBE: 'Adobe',
  PEP: '百事',
  KO: '可口可乐',
  BABA: '阿里巴巴',
  JD: '京东',
  BIDU: '百度',
  PDD: '拼多多',
  NIO: '蔚来',
  XPEV: '小鹏',
  LI: '理想',
  BILI: '哔哩哔哩',
  // 港股（5位数字）
  '00700': '腾讯',
  '09988': '阿里巴巴',
  '03690': '美团',
  '01024': '快手',
  '09888': '百度',
  '09618': '京东',
  '03888': '金山软件',
  '00388': '港交所',
  '00005': '汇丰',
  '00941': '中国移动',
  '00883': '中海油',
  '01299': '友邦',
  '02318': '中国平安',
  '03988': '中国银行',
  '00939': '建行',
}

/** 查询扩展市场代码的中文名，未命中返回大写的代码本身。 */
export function exMarketName(code: string): string {
  return EX_NAME_MAP[code.trim().toUpperCase()] || code.trim().toUpperCase()
}
