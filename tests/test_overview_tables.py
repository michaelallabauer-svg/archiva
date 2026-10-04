"""Regression checks for shared overview rendering; no database access."""
import json
import unittest
from datetime import datetime
from uuid import uuid4

from archiva import ui
from archiva.models import Cabinet, CabinetType, Document, DocumentType, DocType, MetadataField, Register


def archive_fixture(count=35):
    cabinet_type = CabinetType(id=uuid4(), name='Rechnungen', order=0)
    cabinet = Cabinet(id=uuid4(), name='2026', order=0, cabinet_type=cabinet_type)
    register = Register(id=uuid4(), name='Eingang', order=0, cabinet=cabinet)
    fields = [MetadataField(id=uuid4(), name=f'f{i}', label=f'Feld {i}', field_type='text', width='half', order=i) for i in range(12)]
    doc_type = DocumentType(id=uuid4(), name='Rechnung', order=0, register=register, register_id=register.id, fields=fields)
    documents = [Document(id=uuid4(), name=f'rechnung-{i}.pdf', title=f'Rechnung <{i}>',
                          document_type=doc_type, document_type_id=doc_type.id,
                          cabinet=cabinet, cabinet_id=cabinet.id, doc_type=DocType.PDF,
                          created_at=datetime(2026, 10, 4),
                          metadata_json=json.dumps({f'f{j}': 'A & B' for j in range(12)})) for i in range(count)]
    return cabinet_type, cabinet, register, doc_type, documents


class OverviewTablesTest(unittest.TestCase):
    def setUp(self):
        self.ct, self.cabinet, self.register, self.dt, self.docs = archive_fixture()

    def test_all_documents_and_escaped_content_and_correct_actions(self):
        html = ui._render_document_table(self.docs)
        self.assertEqual(html.count('<tr data-overview-row'), len(self.docs))
        self.assertIn('Rechnung &lt;0&gt;', html)
        self.assertIn('A &amp; B', html)
        self.assertNotIn('Rechnung <0>', html)
        for doc in self.docs:
            self.assertIn(f'/ui/app/documents/{doc.id}/delete', html)
            self.assertIn(f'/ui/app/documents/{doc.id}/workflows/start-invoice', html)
        self.assertIn('data-confirm=', html)

    def test_every_archive_node_uses_tables_without_old_result_limits(self):
        for kind, item in [('cabinet_type', self.ct), ('cabinet', self.cabinet),
                           ('register', self.register), ('document_type', self.dt), ('document', self.docs[0])]:
            with self.subTest(kind=kind):
                html, _ = ui._render_node_results([self.cabinet], self.docs,
                    {'kind': kind, 'id': str(item.id), 'label': item.name}, '', {}, [self.dt])
                self.assertIn('search-results-table', html)
                self.assertNotIn('object-card', html)
                if kind != 'cabinet_type':
                    self.assertIn(str(self.docs[-1].id), html)
                if kind == 'document':
                    self.assertEqual(html.count('aria-selected="true"'), 1)

    def test_filters_recent_and_fulltext_overviews_remain_functional(self):
        table, _, _ = ui._render_object_overview(self.docs, search_query='', filter_kind='recent')
        self.assertEqual(table.count('<tr data-overview-row'), 8)
        empty, _, _ = ui._render_object_overview(self.docs, search_query='', filter_kind='untyped')
        self.assertNotIn('<tr data-overview-row', empty)
        search, _ = ui._render_search_results(self.docs, '<test>')
        self.assertEqual(search.count('<tr data-overview-row'), 35)

    def test_structure_actions_target_the_row_not_current_selection(self):
        html = ui._render_structure_table([('cabinet', self.cabinet, 'Test'), ('register', self.register, 'Test')])
        self.assertIn(f'node_id={self.register.id}#metadata-workbench', html)
        self.assertIn(f'/ui/app/cabinets/{self.cabinet.id}/delete', html)
        self.assertIn(f'/ui/app/registers/{self.register.id}/delete', html)
        self.assertIn(f'selected_document_type_id={self.dt.id}', html)

    def test_empty_table_and_trash_restore(self):
        html = ui._render_document_table([])
        self.assertIn('Keine Dokumente gefunden.', html)
        trash = ui._render_admin_trash_page(deleted_documents=[self.docs[0]], deleted_cabinets=[], deleted_registers=[])
        self.assertIn(f'/ui/admin/trash/documents/{self.docs[0].id}/restore', trash)
        self.assertIn('row-action-trigger', trash)
        self.assertNotIn('/delete', trash)


if __name__ == '__main__':
    unittest.main()
