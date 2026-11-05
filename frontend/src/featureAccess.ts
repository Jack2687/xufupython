export const customerFeatures=['home','subscribe','orders','wallet','links','renewal','keys','invites','membership','docs'] as const
export function featureAllowed(features:Record<string,boolean>|undefined,key:string){return key==='profile'||(customerFeatures as readonly string[]).includes(key)&&features?.[key]!==false}
export function routeAllowed(features:Record<string,boolean>|undefined,path:string){return featureAllowed(features,path.slice(1))}
