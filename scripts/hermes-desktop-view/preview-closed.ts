// Upstream LinkifiedText also supports opening local Desktop preview panes.
// No such pane or IPC capability is part of this external read projection.
export const openPreview = () => { throw Error('Desktop preview is unavailable in this read-only view'); };
