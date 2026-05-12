"""RAGFlow parser wrappers for document parsing.

This module provides placeholder implementations for RAGFlow parsers.
RAGFlow is not available as a pip package and requires source installation.

TODO: Replace placeholder implementations with actual RAGFlow integration
      once RAGFlow source is installed. See docs/VERIFICATION_REPORT.md
      for integration instructions.

Required RAGFlow components:
- deepdoc.parser.PdfParser for PDF parsing
- deepdoc.parser.MarkdownParser for Markdown parsing
"""

from .base import DocumentParser, DocumentBlock


class RAGFlowPDFParser(DocumentParser):
    """RAGFlow PDF parser wrapper (placeholder until RAGFlow source integrated).

    This is a placeholder implementation that defines the interface.
    Once RAGFlow source is integrated, this will wrap RAGFlow's deepdoc
    PDF parser which provides:
    - OCR capabilities for scanned documents
    - Layout recognition
    - Table extraction
    - Image extraction
    """

    def __init__(self) -> None:
        """Initialize the PDF parser.

        TODO: Initialize RAGFlow deepdoc PDF parser when available:
              from deepdoc.parser import PdfParser
              self.parser = PdfParser()
        """
        pass

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """Parse PDF and extract text blocks with position info.

        Args:
            file_path: Path to the PDF file

        Returns:
            List of DocumentBlock objects with parsed content

        Raises:
            NotImplementedError: Until RAGFlow source is integrated
        """
        # TODO: Replace with actual RAGFlow parsing:
        # result = self.parser.parse(file_path)
        # return self._convert_to_text_blocks(result)
        raise NotImplementedError(
            "RAGFlow PDF parser requires RAGFlow source installation. "
            "See docs/VERIFICATION_REPORT.md for integration instructions."
        )

    def supports(self, file_path: str) -> bool:
        """Check if file is PDF.

        Args:
            file_path: Path to the file

        Returns:
            True if file has .pdf extension, False otherwise
        """
        return file_path.lower().endswith('.pdf')


class RAGFlowMarkdownParser(DocumentParser):
    """RAGFlow Markdown parser wrapper (placeholder).

    This is a placeholder implementation that defines the interface.
    Once RAGFlow source is integrated, this will wrap RAGFlow's
    Markdown parser.
    """

    def __init__(self) -> None:
        """Initialize the Markdown parser.

        TODO: Initialize RAGFlow markdown parser when available:
              from deepdoc.parser import MarkdownParser
              self.parser = MarkdownParser()
        """
        pass

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """Parse Markdown and extract text blocks.

        Args:
            file_path: Path to the Markdown file

        Returns:
            List of DocumentBlock objects with parsed content

        Raises:
            NotImplementedError: Until RAGFlow source is integrated
        """
        # TODO: Replace with actual RAGFlow parsing:
        # result = self.parser.parse(file_path)
        # return self._convert_to_text_blocks(result)
        raise NotImplementedError(
            "RAGFlow Markdown parser requires RAGFlow source installation."
        )

    def supports(self, file_path: str) -> bool:
        """Check if file is Markdown.

        Args:
            file_path: Path to the file

        Returns:
            True if file has .md or .markdown extension, False otherwise
        """
        return file_path.lower().endswith(('.md', '.markdown'))
