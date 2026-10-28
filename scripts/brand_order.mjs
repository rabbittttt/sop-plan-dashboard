// Keep this presentation order aligned with sop_brand.py.
export const BRAND_ORDER = ['智界', '享界', '尊界', '尚界', '问界'];
const rank = new Map(BRAND_ORDER.map((brand, index) => [brand, index]));

export function compareBrands(left, right) {
  return (rank.get(left) ?? BRAND_ORDER.length) - (rank.get(right) ?? BRAND_ORDER.length)
    || (left < right ? -1 : left > right ? 1 : 0);
}
