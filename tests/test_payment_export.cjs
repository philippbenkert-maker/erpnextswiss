const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('erpnextswiss/erpnextswiss/page/payment_export/payment_export.js', 'utf8');
function setup(names=['PE1']) {
  const calls=[];
  const button={disabled:false, prop(k,v){this[k]=v;return this;},addClass(){return this;}};
  const page={main:{find(selector){
    if(selector.includes(':checked'))return {map(fn){return {get(){return names.map(name=>fn.call({name}));}};}};
    if(selector==='.btn-create-file')return button;
    throw Error('unexpected selector '+selector);
  }}};
  const frappe={pages:{payment_export:{}},call(options){calls.push(options);},msgprint(message){calls.push(message);}};
  const context={frappe,__:s=>s,console};vm.createContext(context);vm.runInContext(source,context);
  return {api:frappe.payment_export,page,button,calls};
}
test('a rapid double click dispatches only one POST and releases on failure',()=>{
  const {api,page,button,calls}=setup();api.export_selected(page);api.export_selected(page);
  assert.equal(calls.length,1);assert.equal(calls[0].type,'POST');assert.equal(button.disabled,true);
  assert.deepEqual(Array.from(calls[0].args.payments),['PE1']);
  calls[0].always();assert.equal(button.disabled,false);assert.equal(api.busy,false);
  api.export_selected(page);assert.equal(calls.length,2);
});
test('empty selection never reaches export endpoint',()=>{
  const {api,page,calls}=setup([]);api.export_selected(page);
  assert.equal(calls.length,1);assert.equal(typeof calls[0],'string');assert.equal(api.busy,undefined);
});
