"""Tests for QR code generation."""

from network.qr_generator import QRGenerator

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_generate_writes_png(tmp_path):
    generator = QRGenerator(output_dir=tmp_path / "qr")
    path = generator.generate("My App", "http://192.168.1.5:3000")
    assert path.exists()
    assert path.read_bytes()[:8] == PNG_MAGIC
    assert path.parent == tmp_path / "qr"


def test_generate_sanitizes_names(tmp_path):
    generator = QRGenerator(output_dir=tmp_path)
    path = generator.generate("My App / v2!!", "http://x:1")
    assert "/" not in path.name
    assert path.exists()


def test_same_name_different_urls_get_distinct_files(tmp_path):
    generator = QRGenerator(output_dir=tmp_path)
    first = generator.generate("Portfolio", "http://192.168.1.5:3000")
    second = generator.generate("Portfolio", "http://192.168.1.5:8080")
    assert first != second
    assert first.exists()
    assert second.exists()
    assert first.read_bytes()[:8] == PNG_MAGIC
    assert second.read_bytes()[:8] == PNG_MAGIC


def test_save_copy(tmp_path):
    generator = QRGenerator(output_dir=tmp_path / "qr")
    source = generator.generate("App", "http://x:1")
    destination = tmp_path / "export" / "code.png"
    result = generator.save_copy(source, destination)
    assert result == destination
    assert destination.read_bytes() == source.read_bytes()
