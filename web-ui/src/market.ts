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

/**
 * 把用户输入转成后端统一的 "市场:代码" 格式，兼容 A 股、美股和港股。
 * 字母代码统一转大写，避免同一美股标的产生多种保存格式。
 */
export function toFullSymbol(code: string): string {
  const c = code.trim()
  if (isExMarketCode(c)) {
    const normalized = /^[A-Za-z]+$/.test(c) ? c.toUpperCase() : c
    return `${detectExMarket(c)}:${normalized}`
  }
  return toSymbol(c)
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
