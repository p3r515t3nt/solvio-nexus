// The original i18n context accepts configClient=null. This build does not
// contain the Desktop API barrel even if a future call accidentally appears.
export const getHermesConfigRecord = async () => { throw Error('Desktop configuration is unavailable in this read-only view'); };
export const saveHermesConfig = getHermesConfigRecord;
